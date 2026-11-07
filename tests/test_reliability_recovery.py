"""Deterministic regression tests for targeted research recovery (item D).

Recovery is driven by *what is missing*: named gaps become targeted queries,
already-attempted queries and failed URLs are not repeated, the round and query
counts are hard limits, and the reason is recorded whether recovery ran or was
skipped. No model and no network: the researcher is a stub.
"""

from __future__ import annotations

import asyncio

import pytest

from athena.agents.sufficiency import assess
from athena.blackboard import Blackboard, EvidenceItem, SQLiteStore, Task
from athena.patterns.recovery import (
    RecoveryReviewAgent,
    attempted_queries,
    build_queries,
    failed_urls,
    plan_recovery,
    recovery_task_text,
)

QUESTION = "Explain how a Hall-effect thruster works and what propellant it uses."
GAPPY_EVIDENCE = EvidenceItem(
    id="fetch-1",
    source_type="web",
    source_ref="https://example.com/electric-propulsion",
    content=(
        "An electric propulsion system uses electric power and it is chosen for "
        "some missions. The page describes spacecraft systems in general terms "
        "without describing the specific device. " * 2
    ),
    retrieved_by="web_researcher",
)
COVERING_EVIDENCE = EvidenceItem(
    id="fetch-1",
    source_type="web",
    source_ref="https://example.com/hall-effect",
    content=(
        "A Hall-effect thruster works by using a magnetic field to trap electrons, "
        "which ionise the propellant; an electric field then accelerates the ions "
        "out of the nozzle. The propellant is typically xenon or krypton gas. "
        "The working principle gives a high specific impulse at low thrust. " * 2
    ),
    retrieved_by="web_researcher",
)
TOOL_REPORT = {
    "calls": [
        {
            "tool": "web_search",
            "ok": False,
            "summary": "{'query': 'effect hall thruster working principle mechanism'}",
        },
        {
            "tool": "fetch_page",
            "ok": False,
            "error": "HTTP 403",
            "requested_url": "https://example.com/paywalled",
        },
    ]
}


def _gappy_assessment():
    return assess(QUESTION, [GAPPY_EVIDENCE], enabled_tools=["web_search", "fetch_page"])


# --- queries come from the gaps, not from the question ----------------------


def test_missing_parts_become_targeted_queries() -> None:
    assessment = _gappy_assessment()
    queries = build_queries(assessment, ["missing parts"])

    assert assessment.missing_aspects  # the fixture really is incomplete
    assert any("working principle mechanism" in query for query in queries)
    assert any("propellant fuel oxidizer" in query for query in queries)


def test_an_unestablished_requirement_becomes_a_release_query() -> None:
    question = "What is the latest stable Python release version?"
    assessment = assess(
        question,
        [
            EvidenceItem(
                id="fetch-1",
                source_type="web",
                source_ref="https://docs.python.org/3/",
                content=(
                    "Python 3.14 is the current stable release of Python and the "
                    "release notes describe the changed behavior. " * 3
                ),
                retrieved_by="web_researcher",
            )
        ],
        enabled_tools=["web_search", "fetch_page"],
    )

    assert assessment.missing_requirements
    queries = build_queries(assessment, ["unestablished requirement"])
    assert any("latest stable version release notes" in query for query in queries)


def test_a_missing_conflict_account_becomes_a_targeted_query() -> None:
    question = (
        "What propellant does SpaceX's Draco thruster use? Some sources describe it "
        "as a nitrogen cold-gas thruster, while others describe a hypergolic engine."
    )
    cold_only = EvidenceItem(
        id="fetch-1",
        source_type="web",
        source_ref="https://example.com/draco-cold-gas",
        content=(
            "The SpaceX Draco thruster is a nitrogen cold gas thruster: pressurised "
            "nitrogen is released through valves and a nozzle to produce thrust. " * 3
        ),
        retrieved_by="web_researcher",
    )
    assessment = assess(question, [cold_only], enabled_tools=["web_search", "fetch_page"])

    queries = build_queries(assessment, ["an unevidenced account"])
    assert any("hypergolic" in query for query in queries), queries


def test_no_query_repeats_a_query_already_attempted() -> None:
    assessment = _gappy_assessment()

    assert attempted_queries(TOOL_REPORT) == [
        ("effect hall thruster working principle mechanism", False)
    ]

    plan = plan_recovery(
        QUESTION, assessment, TOOL_REPORT, available_roles=("web_researcher",)
    )

    attempted = {query for query, _ok in attempted_queries(TOOL_REPORT)}
    assert attempted  # the fixture really contains an attempted query
    assert all(query not in attempted for query in plan.queries)
    assert "effect hall thruster working principle mechanism" in plan.skipped_queries


def test_every_query_being_a_repeat_skips_recovery_with_a_reason() -> None:
    assessment = _gappy_assessment()
    every_candidate = build_queries(assessment, ["x"])
    report = {
        "calls": [
            {"tool": "web_search", "ok": False, "summary": f"{{'query': '{query}'}}"}
            for query in every_candidate
        ]
    }

    plan = plan_recovery(
        QUESTION, assessment, report, available_roles=("web_researcher",)
    )

    assert plan.needed is False
    assert plan.skip_reason == "every targeted query was already attempted"
    assert plan.reason_text().startswith("research recovery skipped:")
    assert set(plan.skipped_queries) <= set(every_candidate)


def test_failed_urls_are_carried_and_not_retried() -> None:
    assert failed_urls(TOOL_REPORT) == ["https://example.com/paywalled"]

    plan = plan_recovery(
        QUESTION, _gappy_assessment(), TOOL_REPORT,
        available_roles=("web_researcher",),
    )
    assert plan.failed_urls == ("https://example.com/paywalled",)

    board = Blackboard.create(Task(question=QUESTION))
    text = recovery_task_text(board, plan, _gappy_assessment())
    assert "URLS THAT ALREADY FAILED (do not retry)" in text
    assert "https://example.com/paywalled" in text
    assert "QUERIES ALREADY ATTEMPTED (do not repeat)" in text


# --- hard limits ------------------------------------------------------------


def test_the_round_limit_is_hard(monkeypatch) -> None:
    monkeypatch.delenv("ATHENA_RECOVERY_MAX_ROUNDS", raising=False)
    assessment = _gappy_assessment()

    first = plan_recovery(QUESTION, assessment, None, available_roles=("web_researcher",))
    assert first.needed is True
    assert first.rounds == 1

    second = plan_recovery(
        QUESTION, assessment, None,
        available_roles=("web_researcher",), rounds_used=first.rounds,
    )
    assert second.needed is False
    assert second.skip_reason == "recovery round limit (1) reached"


def test_the_query_limit_is_hard(monkeypatch) -> None:
    monkeypatch.setenv("ATHENA_RECOVERY_MAX_QUERIES", "1")
    try:
        plan = plan_recovery(
            QUESTION, _gappy_assessment(), None, available_roles=("web_researcher",)
        )
    finally:
        monkeypatch.delenv("ATHENA_RECOVERY_MAX_QUERIES", raising=False)

    assert plan.needed is True
    assert len(plan.queries) == 1
    assert plan.max_queries == 1


def test_a_run_with_no_research_worker_skips_with_a_reason() -> None:
    plan = plan_recovery(QUESTION, _gappy_assessment(), None, available_roles=())

    assert plan.needed is False
    assert "no research worker is enabled" in plan.skip_reason


def test_sufficient_evidence_skips_recovery_and_says_so() -> None:
    assessment = assess(
        QUESTION, [COVERING_EVIDENCE], enabled_tools=["web_search", "fetch_page"]
    )

    plan = plan_recovery(
        QUESTION, assessment, None, available_roles=("web_researcher",)
    )

    assert plan.needed is False
    assert plan.reason_text() == (
        "research recovery skipped: the evidence covers every requested subject and part"
    )


# --- the stage: one bounded round, and the reason is recorded ---------------


class _Stub:
    def __init__(self, name: str, on_run=None, tools=()):
        self.name = name
        self._on_run = on_run
        self.tools = list(tools)

    async def run_async(self, ctx):
        if self._on_run:
            self._on_run()
        yield None


class _Toolset:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _board(tmp_path, question=QUESTION, content=GAPPY_EVIDENCE.content):
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(
        Task(question=question, tools=["web_search", "fetch_page"]), store=store
    )
    board.append(
        "evidence",
        EvidenceItem(
            id="fetch-1",
            source_type="web",
            source_ref="https://example.com/electric-propulsion",
            content=content,
            retrieved_by="web_researcher",
        ),
        author="web_researcher",
    )
    return store, board


def _collect(agent) -> list:
    async def run() -> list:
        return [event async for event in agent._run_async_impl(None)]

    return asyncio.run(run())


def test_one_recovery_round_runs_with_the_gaps_and_records_its_reason(
    tmp_path, monkeypatch
) -> None:
    store, board = _board(tmp_path)
    seen: dict = {}

    def fake_researcher(role, run_id, store, task_text=None):
        seen["role"] = role
        seen["task"] = task_text
        return _Stub(role)

    monkeypatch.setattr("athena.patterns.recovery.build_role_researcher", fake_researcher)
    monkeypatch.setattr(
        "athena.patterns.recovery.build_a2a_stage", lambda role, *a, **k: _Stub(role)
    )

    agent = RecoveryReviewAgent(name="recovery_review", run_id=board.run_id, store=store)
    events = _collect(agent)

    assert seen["role"] == "web_researcher"
    assert "MISSING ANSWER PARTS" in seen["task"]
    assert "WHY THIS RECOVERY ROUND EXISTS" in seen["task"]

    metadata = [
        (event.custom_metadata or {})
        for event in events
        if event is not None and "recovery_rounds" in (event.custom_metadata or {})
    ]
    assert len(metadata) == 1  # exactly one round, one record
    assert metadata[0]["recovery_rounds"] == 1
    assert metadata[0]["recovery_reason"].startswith("research recovery: ")
    assert metadata[0]["recovery_skipped_reason"] == ""


def test_skipped_recovery_records_why_it_was_skipped(tmp_path, monkeypatch) -> None:
    store, board = _board(tmp_path, content=COVERING_EVIDENCE.content)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("recovery must not run when the evidence is complete")

    monkeypatch.setattr(
        "athena.patterns.recovery.build_role_researcher", fail_if_called
    )

    agent = RecoveryReviewAgent(name="recovery_review", run_id=board.run_id, store=store)
    events = _collect(agent)

    metadata = [
        (event.custom_metadata or {})
        for event in events
        if event is not None and "recovery_rounds" in (event.custom_metadata or {})
    ]
    assert metadata[0]["recovery_rounds"] == 0
    assert metadata[0]["recovery_skipped_reason"].startswith(
        "the evidence covers every requested subject and part"
    )


def test_the_recovery_worker_toolset_is_closed_even_when_it_fails(
    tmp_path, monkeypatch
) -> None:
    store, board = _board(tmp_path)
    toolset = _Toolset()

    class Failing(_Stub):
        async def run_async(self, ctx):
            raise RuntimeError("worker crashed")
            yield None  # pragma: no cover

    monkeypatch.setattr(
        "athena.patterns.recovery.build_role_researcher",
        lambda role, *a, **k: Failing(role, tools=[toolset]),
    )

    agent = RecoveryReviewAgent(name="recovery_review", run_id=board.run_id, store=store)

    with pytest.raises(RuntimeError):
        _collect(agent)

    assert toolset.closed is True, "the worker's toolset must not be orphaned"


def test_recovery_is_bounded_to_zero_rounds_by_configuration(
    tmp_path, monkeypatch
) -> None:
    store, board = _board(tmp_path)
    monkeypatch.setenv("ATHENA_RECOVERY_MAX_ROUNDS", "0")
    try:
        monkeypatch.setattr(
            "athena.patterns.recovery.build_role_researcher",
            lambda *a, **k: pytest.fail("no round may run when the limit is 0"),
        )
        agent = RecoveryReviewAgent(
            name="recovery_review", run_id=board.run_id, store=store
        )
        events = _collect(agent)
    finally:
        monkeypatch.delenv("ATHENA_RECOVERY_MAX_ROUNDS", raising=False)

    metadata = [
        (event.custom_metadata or {})
        for event in events
        if event is not None and "recovery_rounds" in (event.custom_metadata or {})
    ]
    assert metadata[0]["recovery_rounds"] == 0
    assert "round limit (0) reached" in metadata[0]["recovery_skipped_reason"]


def test_the_run_summary_carries_the_recovery_reason() -> None:
    """The reason reaches the summary, not just the event log."""
    import athena.run as runner_mod
    from athena.summary import RunSummary
    from types import SimpleNamespace

    summary = RunSummary(pattern="sequential")
    event = SimpleNamespace(
        author="recovery_review",
        content=None,
        custom_metadata={
            "recovery_rounds": 1,
            "recovery_reason": "research recovery: requested part not evidenced: propellant",
            "recovery_skipped_reason": "",
        },
        usage_metadata=None,
    )

    runner_mod._note_event(summary, event, verbose=False)

    assert summary.recovery_rounds == 1
    assert "requested part not evidenced: propellant" in summary.recovery_reason


def test_a_duplicate_source_is_not_counted_twice_by_the_assessment() -> None:
    """Recovery must not "fix" a gap by retrieving the same source again."""
    duplicate = EvidenceItem(
        id="fetch-2",
        source_type="web",
        source_ref=GAPPY_EVIDENCE.source_ref,
        content=GAPPY_EVIDENCE.content,
        retrieved_by="web_researcher",
    )
    assessment = assess(
        QUESTION, [GAPPY_EVIDENCE, duplicate], enabled_tools=["web_search", "fetch_page"]
    )
    single = assess(QUESTION, [GAPPY_EVIDENCE], enabled_tools=["web_search", "fetch_page"])

    assert assessment.distinct_sources == single.distinct_sources
    assert assessment.supported == single.supported
    assert assessment.subject_specific is False
