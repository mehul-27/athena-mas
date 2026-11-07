"""Regression tests: run counters come from the blackboard, not a stale count.

The pilot reported ``evidence_count = 0`` for run
``773aec2a-762d-464b-a970-eebd4c1f35b5`` while ``data/blackboard.db`` held three
real evidence items (two RSC/Wikipedia style web sources and one aggregator) and
three ``append evidence v1/v2/v3`` history rows.

Root cause: the counter was derived from a value read at a moment when it did not
yet hold what the run had recorded, so every benchmark metric downstream scored a
run that did gather evidence as an empty one.

Pinned here: with N evidence items on the blackboard, the summary reports N — for
the counting helpers directly *and* through the real ``athena.run._finalize``
post-run path that the CLI and the evaluation runner both use.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import athena.run as runner_mod
from athena.blackboard import (
    Blackboard,
    Citation,
    Critique,
    EvidenceItem,
    FinalReport,
    SQLiteStore,
    Task,
)
from athena.observability.recorder import Recorder, set_recorder
from athena.summary import (
    RunSummary,
    apply_authoritative_counts,
    count_citations,
    count_evidence,
    set_terminal,
    tools_used_on,
)

QUESTION = "Which element has the chemical symbol W, and what is its atomic number?"
W_URLS = (
    "https://periodic-table.rsc.org/element/74/tungsten",
    "https://en.wikipedia.org/wiki/Tungsten",
    "https://periodic-table.io/element-74",
)


def _board(tmp_path, pattern: str = "hierarchical") -> Blackboard:
    store = SQLiteStore(tmp_path / "bb.db")
    return Blackboard.create(Task(question=QUESTION, pattern=pattern), store=store)


def _record_evidence(blackboard: Blackboard, count: int, author: str = "web_researcher") -> None:
    for index in range(count):
        blackboard.append(
            "evidence",
            EvidenceItem(
                id=f"web-{index}",
                source_type="web",
                source_ref=W_URLS[index % len(W_URLS)],
                content="Tungsten (W) is atomic number 74.",
                retrieved_by=author,
            ),
            author=author,
        )


def _report(blackboard: Blackboard, citations: int = 2, tools: tuple[str, ...] = ("web_search",)) -> None:
    blackboard.write(
        "final_report",
        FinalReport(
            answer="Tungsten (W) is atomic number 74.",
            key_points=["Its chemical symbol is W."],
            citations=[
                Citation(
                    source_type="web",
                    source_ref=W_URLS[index % len(W_URLS)],
                    quote_or_summary="Tungsten has atomic number 74.",
                )
                for index in range(citations)
            ],
            confidence=0.9,
            tools_used=list(tools),
            run_id=blackboard.run_id,
        ),
        author="writer",
    )


# --- the counting helpers ---------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 3, 7])
def test_evidence_count_equals_what_the_blackboard_holds(tmp_path, count) -> None:
    blackboard = _board(tmp_path)
    _record_evidence(blackboard, count)

    assert count_evidence(blackboard) == count


def test_the_pilot_run_shape_reports_three_not_zero(tmp_path) -> None:
    """The exact evidence the pilot recorded, counted from the store."""
    blackboard = _board(tmp_path)
    _record_evidence(blackboard, 3)

    summary = RunSummary(pattern="hierarchical", run_id=blackboard.run_id)
    apply_authoritative_counts(summary, blackboard)

    assert summary.evidence_count == 3
    # A derived counter left at its default cannot survive the overwrite.
    assert summary.evidence_count != 0


def test_citations_and_tools_used_are_read_from_the_report(tmp_path) -> None:
    blackboard = _board(tmp_path)
    _record_evidence(blackboard, 3)
    _report(blackboard, citations=2, tools=("web_search", "fetch_page"))

    assert count_citations(blackboard) == 2
    assert tools_used_on(blackboard) == ["web_search", "fetch_page"]

    summary = RunSummary(pattern="hierarchical")
    apply_authoritative_counts(summary, blackboard)
    assert (summary.citation_count, summary.tools_used) == (
        2, ["web_search", "fetch_page"]
    )


def test_an_unwritten_section_stays_an_honest_zero(tmp_path) -> None:
    blackboard = _board(tmp_path)

    summary = RunSummary(pattern="hierarchical")
    apply_authoritative_counts(summary, blackboard)

    assert summary.evidence_count == 0
    assert summary.citation_count == 0
    assert summary.tools_used == []


def test_evidence_recorded_by_any_author_is_counted(tmp_path) -> None:
    """The store is authoritative, whoever appended: ADK worker or CrewAI role."""
    blackboard = _board(tmp_path)
    _record_evidence(blackboard, 2, author="web_researcher")
    _record_evidence(blackboard, 2, author="rag_researcher")

    assert count_evidence(blackboard) == 4


# --- through the real post-run path the CLI and the harness both use --------


class _Sessions:
    async def create_session(self, **_kwargs):
        return SimpleNamespace(id="session-1")


class _Runner:
    """Writes evidence, then a report and critique, like a real run does."""

    def __init__(self, run_id: str, evidence: int, critique: bool = True) -> None:
        self.session_service = _Sessions()
        self.run_id = run_id
        self.evidence = evidence
        self.critique = critique

    async def run_async(self, **_kwargs):
        board = Blackboard.open(self.run_id)
        for index in range(self.evidence):
            board.append(
                "evidence",
                EvidenceItem(
                    id=f"fetch-{index}",
                    source_type="web",
                    source_ref=W_URLS[index % len(W_URLS)],
                    content="Tungsten (W) is atomic number 74, a metal. " * 4,
                    retrieved_by="web_researcher",
                ),
                author="web_researcher",
            )
        if self.evidence:
            board.write(
                "final_report",
                FinalReport(
                    answer="Tungsten (W) has atomic number 74.",
                    key_points=["Its symbol is W."],
                    citations=[
                        Citation(
                            source_type="web",
                            source_ref=W_URLS[0],
                            quote_or_summary="Tungsten, atomic number 74.",
                        )
                    ],
                    confidence=0.9,
                    tools_used=["web_search", "fetch_page"],
                    run_id=self.run_id,
                ),
                author="writer",
            )
        if self.critique:
            board.write(
                "critique",
                Critique(approved=True, severity="low", issues=[]),
                author="critic",
            )
        yield SimpleNamespace(
            author="supervisor", content=None, custom_metadata={"final": True},
            usage_metadata=None,
        )


def _execute(tmp_path, evidence: int) -> RunSummary:
    recorder = Recorder("unused", None, enabled=False)
    set_recorder(recorder)
    try:
        return asyncio.run(
            runner_mod._execute(
                QUESTION, "hierarchical", verbose=False,
                runner_factory=lambda run_id, pattern, _rec: _Runner(run_id, evidence),
            )
        )
    finally:
        set_recorder(None)


@pytest.mark.parametrize("count", [1, 3])
def test_finalize_reports_the_evidence_the_run_recorded(tmp_path, count) -> None:
    summary = _execute(tmp_path, count)

    assert summary.evidence_count == count
    assert summary.citation_count == 1
    assert summary.tools_used == ["web_search", "fetch_page"]
    # And it is the same number the blackboard holds for that run_id.
    blackboard = Blackboard.open(summary.run_id)
    assert len(blackboard.read("evidence")[0]) == count


def test_finalize_counts_nothing_for_a_run_with_no_evidence(tmp_path) -> None:
    summary = _execute(tmp_path, 0)

    assert summary.evidence_count == 0
    assert summary.citation_count == 0


def test_the_written_json_summary_carries_the_real_counters(tmp_path) -> None:
    """The JSON file the benchmark harness reads says 3, not 0."""
    summary = _execute(tmp_path, 3)
    target = tmp_path / "summary.json"
    runner_mod.write_summary_json(summary, str(target))

    import json

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["evidence_count"] == 3
    assert payload["citation_count"] == 1
    assert payload["tools_used"] == ["web_search", "fetch_page"]


def test_a_cancelled_run_still_reports_its_recorded_counts(tmp_path) -> None:
    """The counter fix is not conditional on a clean ending."""
    blackboard = _board(tmp_path)
    _record_evidence(blackboard, 3)
    summary = RunSummary(pattern="hierarchical", run_id=blackboard.run_id)
    set_terminal(summary, "cancelled", "the run was cancelled")

    apply_authoritative_counts(summary, blackboard)
    assert summary.evidence_count == 3
    assert summary.terminal_state == "cancelled"
