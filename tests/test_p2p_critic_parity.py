"""Focused P2P critic parity: calibration, citation audit, count scope, revision.

Everything above the fake model, planner, researcher and A2A transport is the
real P2P supervisor, context builder and role handlers. No network or model
call is made.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from athena.agents import crew_p2p
from athena.blackboard import (
    Analysis,
    Blackboard,
    Citation,
    Critique,
    EvidenceItem,
    FinalReport,
    SQLiteStore,
    Task,
)
from athena.patterns import p2p as p2p_module
from athena.patterns.context import P2PContext, context_budget

QUESTION = "What is the SpaceX Draco thruster?"
OVERCLAIM = "The Draco thruster is used on every flight."
CORRECTED = "The SpaceX Draco thruster is planned for the capsule."
DRACO_CONTENT = (
    "The SpaceX Draco thruster is planned for the capsule. It is a propulsion "
    "device under development. "
) * 3
NINTH_REF = "https://example.org/cited-ninth"


class _Stub:
    def __init__(self, items=(), sink=None):
        self.items = list(items)
        self.sink = sink

    async def run_async(self, ctx):
        if self.sink is not None:
            self.sink(self.items)
        for event in ():
            yield event


def _draco_item(identifier: str = "fetch-1") -> EvidenceItem:
    return EvidenceItem(
        id=identifier,
        source_type="web",
        source_ref="https://example.org/draco",
        content=DRACO_CONTENT,
        retrieved_by="web_researcher",
    )


def _unrelated(index: int) -> EvidenceItem:
    return EvidenceItem(
        id=f"unrelated-{index}",
        source_type="rag",
        source_ref="doc://first/source",
        content=f"Unrelated evidence item {index}.",
        retrieved_by="researcher",
    )


def _report(answer: str, citations=None) -> FinalReport:
    return FinalReport(
        answer=answer,
        key_points=[],
        citations=list(citations or []),
        confidence=0.8,
        run_id="r",
    )


def _metadata(events) -> dict:
    for event in events:
        metadata = event.custom_metadata or {}
        if "revision_rounds" in metadata:
            return metadata
    raise AssertionError("the supervisor did not emit revision metadata")


def _run_supervisor(
    tmp_path,
    monkeypatch,
    *,
    question: str = QUESTION,
    evidence=None,
    writer,
    critic,
):
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question=question), store=store)
    prompts: dict[str, list[str]] = {"analyst": [], "writer": [], "critic": []}
    payloads: list[dict] = []
    researcher_tasks: list[str] = []

    def fake_crew_call(role, goal, prompt, schema):
        prompts[role].append(prompt)
        index = len(prompts[role]) - 1
        if role == "analyst":
            model = Analysis(key_findings=["The thruster is planned."], gaps=[])
        elif role == "writer":
            model = writer(index)
        else:
            model = critic(index)
        return model, {"tokens_in": 1, "tokens_out": 1}

    monkeypatch.setattr(crew_p2p, "crew_call", fake_crew_call)

    async def fake_send(card_url, payload, timeout=None):
        body = json.loads(payload)
        payloads.append(body)
        result = crew_p2p.P2P_ROLES[body["role"]](
            body["run_id"], body["instruction"], store=None, context=body["context"]
        )
        return json.dumps({"ok": True, "role": body["role"], **result})

    monkeypatch.setattr(p2p_module, "send_role_request", fake_send)
    monkeypatch.setattr(p2p_module, "get_recorder", lambda: None)

    initial = list(evidence if evidence is not None else [_draco_item()])

    def fake_researcher(run_id, store=None, task_text="", sink=None):
        researcher_tasks.append(task_text)
        items = initial if len(researcher_tasks) == 1 else []
        return _Stub(items, sink)

    monkeypatch.setattr(p2p_module, "build_researcher", fake_researcher)
    monkeypatch.setattr(p2p_module, "build_planner", lambda *a, **k: _Stub())
    monkeypatch.setattr(
        p2p_module,
        "build_tool_report",
        lambda *a, **k: {
            "calls": [],
            "failed": [],
            "enabled": [],
            "assignments": {},
            "planned": [],
            "invoked": [],
            "usable_evidence": [],
            "no_usable_evidence": [],
            "mixed": [],
            "telemetry_conflicts": [],
            "blocked": {},
            "blocked_counts": {},
            "skipped": [],
        },
    )
    monkeypatch.setattr(
        p2p_module.registry, "task_selection", lambda *a, **k: None
    )

    agent = p2p_module.P2PAgent(
        name="supervisor",
        run_id=board.run_id,
        question=question,
        store=store,
    )

    async def drive():
        return [event async for event in agent._run_async_impl(None)]

    events = asyncio.run(drive())
    return SimpleNamespace(
        board=board,
        events=events,
        prompts=prompts,
        payloads=payloads,
        researcher_tasks=researcher_tasks,
    )


def test_overclaim_is_rejected_even_when_remote_critic_approves(
    tmp_path, monkeypatch
) -> None:
    """G1: calibration uses inline evidence; the raw approval cannot survive."""

    def writer(index: int) -> FinalReport:
        return _report(OVERCLAIM if index == 0 else CORRECTED)

    def critic(_index: int) -> Critique:
        return Critique(issues=[], severity="low", approved=True)

    result = _run_supervisor(
        tmp_path, monkeypatch, writer=writer, critic=critic
    )

    assert len(result.prompts["writer"]) == 2, "one bounded prose revision"
    assert len(result.prompts["critic"]) == 2
    assert len(result.researcher_tasks) == 1, "no research when evidence is sufficient"

    assert "PREVIOUS CRITIQUE" not in result.prompts["writer"][0]
    assert "PREVIOUS CRITIQUE" in result.prompts["writer"][1]
    assert "claim exceeds the recorded evidence" in result.prompts["writer"][1]
    assert "planned capability stated as existing" in result.prompts["writer"][1]

    final = result.board.read("critique")[0]
    assert final.approved is True
    assert final.severity == "low"
    metadata = _metadata(result.events)
    assert metadata["revision_rounds"] == 1
    assert metadata["recovery_rounds"] == 0


def test_persistent_high_rejection_gets_exactly_one_revision_and_stays_rejected(
    tmp_path, monkeypatch
) -> None:
    """G4: the revision is bounded and never auto-approves."""

    def writer(_index: int) -> FinalReport:
        return _report(OVERCLAIM)

    def critic(_index: int) -> Critique:
        return Critique(issues=[], severity="low", approved=True)

    result = _run_supervisor(
        tmp_path, monkeypatch, writer=writer, critic=critic
    )

    assert len(result.prompts["writer"]) == 2
    assert len(result.prompts["critic"]) == 2
    assert len(result.researcher_tasks) == 1
    assert "claim exceeds the recorded evidence" in result.prompts["writer"][1]

    final = result.board.read("critique")[0]
    assert final.approved is False
    assert final.severity == "high"
    assert any("planned capability stated as existing" in issue for issue in final.issues)
    assert _metadata(result.events)["revision_rounds"] == 1


@pytest.mark.parametrize(
    "verdict",
    [
        Critique(issues=[], severity="low", approved=True),
        Critique(issues=["Minor style issue."], severity="low", approved=False),
        Critique(issues=["A medium concern."], severity="medium", approved=False),
    ],
    ids=["approved", "low-rejected", "medium-rejected"],
)
def test_low_medium_and_approved_verdicts_do_not_trigger_revision(
    tmp_path, monkeypatch, verdict
) -> None:
    def writer(_index: int) -> FinalReport:
        return _report(CORRECTED)

    def critic(_index: int) -> Critique:
        return verdict

    result = _run_supervisor(
        tmp_path, monkeypatch, writer=writer, critic=critic
    )

    assert len(result.prompts["writer"]) == 1
    assert len(result.prompts["critic"]) == 1
    assert len(result.researcher_tasks) == 1
    assert _metadata(result.events)["revision_rounds"] == 0
    final = result.board.read("critique")[0]
    assert final.approved is verdict.approved
    assert final.severity == verdict.severity


def test_insufficient_assessment_recovers_with_the_calibrated_critique(
    tmp_path, monkeypatch
) -> None:
    """G1: recovery reads the calibrated verdict, not the raw remote approval."""
    question = "What is the SpaceX Draco thruster used for?"

    def writer(_index: int) -> FinalReport:
        return _report(OVERCLAIM)

    def critic(_index: int) -> Critique:
        return Critique(issues=[], severity="low", approved=True)

    result = _run_supervisor(
        tmp_path, monkeypatch, question=question, writer=writer, critic=critic
    )

    assert len(result.researcher_tasks) == 2, "exactly one research recovery pass"
    recovery_task = result.researcher_tasks[1]
    assert "claim exceeds the recorded evidence" in recovery_task
    assert "planned capability stated as existing" in recovery_task
    metadata = _metadata(result.events)
    assert metadata["recovery_rounds"] == 1
    assert metadata["revision_rounds"] == 0


def test_critic_prioritizes_even_a_ninth_cited_source_and_audits_it(
    tmp_path, monkeypatch
) -> None:
    """G3: the cited source survives the eight-item cap; the audit is honest."""
    ninth = EvidenceItem(
        id="fetch-cited-ninth",
        source_type="web",
        source_ref=NINTH_REF,
        content=DRACO_CONTENT,
        retrieved_by="web_researcher",
    )
    evidence = [_unrelated(index) for index in range(8)] + [ninth]

    def writer(_index: int) -> FinalReport:
        return _report(
            CORRECTED,
            citations=[
                Citation(
                    source_type="web",
                    source_ref=NINTH_REF,
                    quote_or_summary="The thruster is planned.",
                )
            ],
        )

    def critic(_index: int) -> Critique:
        return Critique(issues=[], severity="low", approved=True)

    result = _run_supervisor(
        tmp_path, monkeypatch, evidence=evidence, writer=writer, critic=critic
    )

    critic_payload = [body for body in result.payloads if body["role"] == "critic"][0]
    context = critic_payload["context"]
    assert "[fetch-cited-ninth]" in context
    assert "Citation 1: recorded and excerpted" in context
    evidence_lines = [line for line in context.splitlines() if line.startswith("[")]
    assert len(evidence_lines) <= 8
    audit = (
        context.split("Citation reference audit", 1)[1]
        .split("Evidence (report citations prioritized):", 1)[0]
    )
    assert NINTH_REF not in audit, "the audit must not echo report-controlled refs"


def test_citation_audit_reflects_actual_character_trimming() -> None:
    """G3: an excerpt dropped by the char budget is not called excerpted."""
    item = EvidenceItem(
        id="fetch-huge",
        source_type="web",
        source_ref=NINTH_REF,
        content="x" * 2000,
        retrieved_by="web_researcher",
    )
    report = _report(
        "Supported answer.",
        citations=[
            Citation(
                source_type="web",
                source_ref=NINTH_REF,
                quote_or_summary="A source.",
            )
        ],
    ).model_dump_json()
    context = P2PContext(question="q", evidence=[item], report=report)

    trimmed = context.evidence_block(limit=400)
    assert "recorded but outside the evidence excerpt budget" in trimmed
    assert "[fetch-huge]" not in trimmed
    assert len(trimmed) <= 400

    full = context.evidence_block(limit=3000)
    assert "recorded and excerpted" in full
    assert "[fetch-huge]" in full


def test_citation_audit_reports_missing_sources_when_no_evidence_was_recorded() -> None:
    report = _report(
        "A claim with an unrecorded citation.",
        citations=[Citation(source_type="web", source_ref=NINTH_REF,
                            quote_or_summary="A source.")],
    )
    context = P2PContext(question="q", report=report.model_dump_json())
    block = context.evidence_block(limit=900)
    assert "Citation 1: missing from recorded evidence" in block
    assert "no citable evidence recorded" in block
    assert "recorded and excerpted" not in block
    assert len(block) <= 900


def test_critic_context_preserves_the_full_report_and_stays_within_the_cap() -> None:
    report = _report(
        "A supported answer.",
        citations=[
            Citation(
                source_type="web",
                source_ref=NINTH_REF,
                quote_or_summary="A source.",
            )
        ],
    ).model_dump_json()
    evidence = [
        EvidenceItem(
            id=f"fetch-{index}",
            source_type="web",
            source_ref=f"https://example.org/{index}",
            content="A relevant long body. " * 100,
            retrieved_by="web_researcher",
        )
        for index in range(12)
    ]
    context = P2PContext(
        question="q", evidence=evidence, analysis="ANALYSIS", report=report
    )

    text = context.for_role("critic")

    assert report in text
    assert len(text) <= context_budget()
    assert len(text) <= 9000


def test_writer_count_ignores_a_count_that_appears_only_in_evidence(
    tmp_path, monkeypatch
) -> None:
    """G2: the evidence says 'three'; the question asks for no count."""
    evidence = [
        EvidenceItem(
            id="fetch-1",
            source_type="web",
            source_ref="https://example.org/draco",
            content="The paper lists three key changes. " + DRACO_CONTENT,
            retrieved_by="web_researcher",
        )
    ]

    def writer(_index: int) -> FinalReport:
        return _report(CORRECTED)

    def critic(_index: int) -> Critique:
        return Critique(issues=[], severity="low", approved=True)

    result = _run_supervisor(
        tmp_path, monkeypatch, evidence=evidence, writer=writer, critic=critic
    )

    writer_prompt = result.prompts["writer"][0]
    assert "3-5 key points" in writer_prompt
    assert "exactly 3 key points" not in writer_prompt
    assert "The question requires exactly 3 findings" not in writer_prompt


def test_explicit_question_count_is_still_enforced(monkeypatch) -> None:
    captured: list[str] = []

    def fake_crew_call(_role, _goal, prompt, _schema):
        captured.append(prompt)
        return _report(CORRECTED), {"tokens_in": 1, "tokens_out": 1}

    monkeypatch.setattr(crew_p2p, "crew_call", fake_crew_call)
    context = P2PContext(question="List three key changes for the Draco thruster.")

    crew_p2p.p2p_write_report(
        "r", "Using the task, write the report.", context=context.for_role("writer")
    )

    prompt = captured[0]
    assert "The question requires exactly 3 findings" in prompt
    assert "exactly 3 key points" in prompt
    assert "at least 3 citations from distinct sources" in prompt
