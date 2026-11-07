"""Deterministic regression tests for completeness-aware confidence (item C).

The writer's self-reported number is never trusted: the shipped confidence is
recomputed from the evidence assessment, the claim checks and the critic verdict,
*after* sanitization and *after* the review. These tests pin that ordering and the
"latest stable release" behaviour (a patch version the evidence never states is a
missing requirement, and it caps confidence).
"""

from __future__ import annotations

import asyncio

from athena.agents.confidence import (
    CEILING_CRITIC_HIGH,
    compute_confidence,
    finalize_confidence,
)
from athena.agents.sufficiency import assess
from athena.agents.supervisor import SupervisorAgent
from athena.blackboard import (
    Blackboard,
    Citation,
    Critique,
    EvidenceItem,
    FinalReport,
    SQLiteStore,
    Task,
)

QUESTION = "What is the latest stable Python release version? What changed in it?"
SOURCE = "https://docs.python.org/3/"
SHALLOW = (
    "Python 3.14 is the current stable release of Python. The release notes describe "
    "the changed behavior and the new features in that release. "
)
DEEP = (
    "Python 3.14.2 is the current stable release of Python. The release notes "
    "describe the changed behavior and the new features in that release. "
)


def _evidence(content: str = SHALLOW) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            id="fetch-1",
            source_type="web",
            source_ref=SOURCE,
            content=content * 3,
            retrieved_by="web_researcher",
        )
    ]


def _report(confidence: float = 0.95, answer: str | None = None) -> FinalReport:
    return FinalReport(
        answer=answer or "Python 3.14 is the current stable release of Python.",
        key_points=[],
        citations=[
            Citation(
                source_type="web",
                source_ref=SOURCE,
                quote_or_summary="Python 3.14 is the current stable release",
            )
        ],
        confidence=confidence,
        run_id="run-confidence",
    )


# --- the missing patch version ----------------------------------------------


def test_latest_patch_version_is_a_missing_requirement_without_a_patch_number() -> None:
    assessment = assess(QUESTION, _evidence(), enabled_tools=["web_search", "fetch_page"])

    assert assessment.missing_requirements
    assert "patch version" in assessment.missing_requirements[0]
    assert assessment.answer_as_asked is False
    assert assessment.ok is False
    assert assessment.confidence_ceiling <= 0.45
    assert any("patch version" in issue for issue in assessment.issues)


def test_an_established_patch_version_satisfies_the_requirement() -> None:
    assessment = assess(
        QUESTION, _evidence(DEEP), enabled_tools=["web_search", "fetch_page"]
    )

    assert assessment.missing_requirements == ()
    assert assessment.confidence_ceiling > 0.45


def test_confidence_is_capped_at_the_evidence_ceiling_and_says_why() -> None:
    items = _evidence()
    assessment = assess(QUESTION, items, enabled_tools=["web_search", "fetch_page"])

    decision = compute_confidence(_report(0.95), assessment, None, items, QUESTION)

    assert decision.previous == 0.95
    assert decision.confidence <= assessment.confidence_ceiling
    assert decision.confidence == 0.45
    assert decision.changed is True
    assert any("patch version" in note for note in decision.notes)
    assert "lowered from 0.95" in decision.summary()


def test_a_report_below_the_ceiling_is_left_alone() -> None:
    items = _evidence()
    assessment = assess(QUESTION, items, enabled_tools=["web_search", "fetch_page"])

    decision = compute_confidence(_report(0.2), assessment, None, items, QUESTION)

    assert decision.confidence == 0.2
    assert decision.changed is False


# --- the critic and the claim checks can only lower it ----------------------


def test_a_rejecting_critic_caps_confidence_further() -> None:
    items = _evidence(DEEP)
    assessment = assess(QUESTION, items, enabled_tools=["web_search", "fetch_page"])
    critique = Critique(approved=False, severity="high", issues=["unsupported claims"])

    decision = compute_confidence(_report(0.9), assessment, critique, items, QUESTION)

    assert decision.confidence <= CEILING_CRITIC_HIGH
    assert ("critic", CEILING_CRITIC_HIGH) in decision.factors
    assert any("critic rejected" in note for note in decision.notes)


def test_unsupported_claims_and_missing_citations_cap_confidence() -> None:
    items = _evidence(DEEP)
    assessment = assess(QUESTION, items, enabled_tools=["web_search", "fetch_page"])
    bad = FinalReport(
        answer="Python 3.14.7 shipped in 2027 with a rewritten compiler.",
        key_points=[],
        citations=[],
        confidence=0.9,
        run_id="run-confidence",
    )

    decision = compute_confidence(bad, assessment, None, items, QUESTION)

    assert decision.confidence <= 0.4
    assert any("lack recorded support" in note for note in decision.notes)
    assert any("without citations" in note for note in decision.notes)


def test_confidence_never_exceeds_the_ceiling_whatever_the_writer_claims() -> None:
    items = _evidence(DEEP)
    assessment = assess(QUESTION, items, enabled_tools=["web_search", "fetch_page"])

    for claimed in (0.0, 0.5, 0.99, 1.0):
        decision = compute_confidence(_report(claimed), assessment, None, items, QUESTION)
        assert decision.confidence <= decision.ceiling <= 1.0
        assert decision.confidence <= claimed


# --- recomputation happens after sanitization and after the review ---------


def test_finalize_confidence_writes_the_capped_number_back(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(
        Task(question=QUESTION, tools=["web_search", "fetch_page"]), store=store
    )
    board.append("evidence", _evidence()[0], author="web_researcher")
    board.write("final_report", _report(0.95), author="writer")

    decision = finalize_confidence(board, board.run_id, emit=False)

    assert decision is not None
    shipped = board.read("final_report")[0].confidence
    assert shipped == decision.confidence == 0.45

    # Idempotent: a second pass neither raises the number nor bumps the version.
    version = board.read("final_report")[1]
    again = finalize_confidence(board, board.run_id, emit=False)
    assert again is not None and again.changed is False
    assert board.read("final_report")[1] == version


def test_the_supervised_run_ships_the_reviewed_confidence_not_the_writers(tmp_path) -> None:
    """The confidence is recomputed after the critic, not before.

    The writer claims 0.95. The critic rejects the report with high severity. Only
    a recomputation that runs *after* the review can report 0.5, so this pins the
    ordering in the supervisor.
    """
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(
        Task(question=QUESTION, tools=["web_search", "fetch_page"]), store=store
    )
    board.append("evidence", _evidence(DEEP)[0], author="web_researcher")

    class Stub:
        def __init__(self, name, fn):
            self.name = name
            self.fn = fn

        async def run_async(self, ctx):
            self.fn()
            yield None

    def write_report() -> None:
        board.write("final_report", _report(0.95), author="writer")

    def write_critique() -> None:
        board.write(
            "critique",
            Critique(approved=False, severity="high", issues=["unsupported claims"]),
            author="critic",
        )

    agent = SupervisorAgent(
        name="supervisor",
        run_id=board.run_id,
        store=store,
        stages=[Stub("writer", write_report), Stub("critic", write_critique)],
    )

    async def run_supervisor() -> list:
        return [event async for event in agent._run_async_impl(None)]

    events = asyncio.run(run_supervisor())

    assert board.read("final_report")[0].confidence == CEILING_CRITIC_HIGH
    final = [
        (event.custom_metadata or {})
        for event in events
        if event is not None and (event.custom_metadata or {}).get("final")
    ]
    assert final and final[0]["confidence"] == CEILING_CRITIC_HIGH
    assert any("critic" in note for note in final[0]["confidence_notes"])
