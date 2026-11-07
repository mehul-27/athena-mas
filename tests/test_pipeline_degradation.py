"""Regression tests: ``degraded`` means something actually degraded the run.

The pilot run ``cafa1b22-77d1-4a03-8e5a-9c6d1f0b7a42`` ended
``outcome=approved`` / ``terminal_state=success`` with the critic's approval, six
recorded evidence items, real Wikipedia/RSC/periodic-table citations and a
352-character answer — and the summary still said ``degraded = True`` with **no
reason at all**. The only thing that had gone wrong was one ``fetch_page`` call
answering HTTP 403 while the same stage went on to fetch two other pages and
gather every piece of evidence the answer needed.

So a health flag was being raised for a failure the run had absorbed, and no
consumer could tell why. Both halves are pinned here:

* an approved run with evidence and citations is **not** degraded, its
  ``degraded_reason`` is empty, and its counters match the shipped report;
* a run that really lost something — a toolset, a stage, a budget — **is**
  degraded and carries a non-empty reason naming the loss.

Everything is offline: the runner is a stub that writes to a real blackboard and
a real event store, and no model, tool or server is contacted.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import athena.run as runner_mod
from athena.agents import toolset_health
from athena.blackboard import (
    Blackboard,
    Citation,
    Critique,
    EvidenceItem,
    FinalReport,
    SQLiteStore,
    Task,
)
from athena.observability.recorder import Recorder, get_recorder, set_recorder
from athena.observability.store import SQLiteEventStore
from athena.summary import (
    RunSummary,
    finish,
    mark_degraded,
    note_recovered,
    set_outcome,
    summary_line,
    unrecovered_errors,
)

QUESTION = "What propellant does the SpaceX Draco thruster use, and what is its specific impulse?"
# The three sources the real pilot actually cited.
PILOT_URLS = (
    "https://en.wikipedia.org/wiki/SpaceX_Draco",
    "https://www.rsc.org/periodic-table/",
    "https://www.periodic-table.org/",
)
EVIDENCE_BODY = (
    "Draco is a hypergolic bipropellant thruster using monomethylhydrazine fuel "
    "and a nitrogen tetroxide oxidiser, with a vacuum specific impulse near 300 s. "
)


# --- fixtures --------------------------------------------------------------


class _Sessions:
    async def create_session(self, **_kwargs):
        return SimpleNamespace(id="session-1")


def _final_event() -> SimpleNamespace:
    return SimpleNamespace(
        author="supervisor", content=None, custom_metadata={"final": True},
        usage_metadata=None,
    )


def _record_tool_call(
    run_id: str,
    *,
    agent: str,
    tool: str,
    args: str,
    ok: bool,
    error: str = "",
    blocked_by: str = "",
) -> None:
    """Write one ``tool_call`` event through the recorder the run installed."""
    recorder = get_recorder()
    assert recorder is not None, "the run must have installed a recorder"
    extra = {"tool": tool, "agent": agent}
    if blocked_by:
        extra["blocked_by"] = blocked_by
    recorder.emit(
        "tool_call",
        agent,
        "adk",
        ok=ok,
        duration_ms=12.5,
        error_type=None if ok else "ToolError",
        error_message=error or None,
        payload_summary=f"{tool}: {args}",
        extra=extra,
    )


class _PilotRunner:
    """A run shaped exactly like the successful pilot.

    One fetch fails with HTTP 403; two other fetches and a search succeed; six
    evidence items, a cited report and an approving critique land on the
    blackboard. This is the run that must not be called degraded.
    """

    def __init__(self, run_id: str, *, evidence: int = 6, citations: int = 3,
                 critique: bool = True, failed_fetch: bool = True) -> None:
        self.session_service = _Sessions()
        self.run_id = run_id
        self.evidence = evidence
        self.citations = citations
        self.critique = critique
        self.failed_fetch = failed_fetch

    async def run_async(self, **_kwargs):
        agent = "web_researcher"
        _record_tool_call(
            self.run_id, agent=agent, tool="web_search",
            args="{'query': 'draco propellant specific impulse', 'max_results': 3}", ok=True,
        )
        if self.failed_fetch:
            _record_tool_call(
                self.run_id, agent=agent, tool="fetch_page",
                args="{'url': 'https://en.wikipedia.org/wiki/SpaceX_Draco'}",
                ok=False, error="HTTP 403",
            )
        for url in PILOT_URLS[1:]:
            _record_tool_call(
                self.run_id, agent=agent, tool="fetch_page",
                args=f"{{'url': '{url}'}}", ok=True,
            )

        board = Blackboard.open(self.run_id)
        for index in range(self.evidence):
            board.append(
                "evidence",
                EvidenceItem(
                    id=f"web-{index}",
                    source_type="web",
                    source_ref=PILOT_URLS[index % len(PILOT_URLS)],
                    content=EVIDENCE_BODY,
                    retrieved_by=agent,
                ),
                author=agent,
            )
        board.write(
            "final_report",
            FinalReport(
                answer=(
                    "Draco burns monomethylhydrazine and nitrogen tetroxide, a "
                    "hypergolic pair, giving roughly 300 s of specific impulse in "
                    "vacuum, which is why it needs no ignition system."
                ),
                key_points=["Hypergolic MMH/NTO propellant pair."],
                citations=[
                    Citation(
                        source_type="web",
                        source_ref=PILOT_URLS[index % len(PILOT_URLS)],
                        quote_or_summary="Draco propellant and specific impulse.",
                    )
                    for index in range(self.citations)
                ],
                confidence=0.55,
                tools_used=["fetch_page", "web_search"],
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
        yield _final_event()


def _execute(factory) -> RunSummary:
    return asyncio.run(
        runner_mod._execute(
            QUESTION, "hierarchical", verbose=False, runner_factory=factory
        )
    )


# --- (a) the successful pilot is not degraded ------------------------------


def test_the_approved_pilot_run_is_not_degraded_and_says_why_not() -> None:
    """The exact pilot shape: approved, six evidence items, three citations."""
    summary = _execute(lambda run_id, pattern, recorder: _PilotRunner(run_id))

    assert summary.outcome == "approved"
    assert summary.terminal_state == "success"
    assert summary.approved is True
    assert summary.critic_status == "approved"
    assert summary.evidence_count == 6
    assert summary.failed is False

    # The defect: this was True with an empty reason.
    assert summary.degraded is False
    assert summary.degraded_reason == ""


def test_the_failed_fetch_is_recorded_but_not_a_degradation() -> None:
    """A 403 that the stage absorbed is reported, and costs the run nothing."""
    summary = _execute(lambda run_id, pattern, recorder: _PilotRunner(run_id))

    assert any("HTTP 403" in error for error in summary.stage_errors)
    assert any("HTTP 403" in error for error in summary.recovered_errors)
    assert unrecovered_errors(summary) == []
    assert summary.degraded is False


def test_an_approved_run_with_no_failures_at_all_is_not_degraded() -> None:
    summary = _execute(
        lambda run_id, pattern, recorder: _PilotRunner(run_id, failed_fetch=False)
    )

    assert summary.degraded is False
    assert summary.degraded_reason == ""
    assert summary.stage_errors == []


@pytest.mark.parametrize("citations", [1, 3, 5])
def test_citation_count_is_the_length_of_the_shipped_report(citations) -> None:
    """The pilot summary said 0 while the report carried 3, exactly like evidence_count."""
    summary = _execute(
        lambda run_id, pattern, recorder: _PilotRunner(run_id, citations=citations)
    )

    blackboard = Blackboard.open(summary.run_id)
    report = blackboard.read("final_report")[0]
    assert len(report.citations) == citations
    assert summary.citation_count == citations
    assert summary.citation_count != 0


def test_every_counter_describes_the_report_that_shipped() -> None:
    summary = _execute(lambda run_id, pattern, recorder: _PilotRunner(run_id))
    report = Blackboard.open(summary.run_id).read("final_report")[0]

    assert summary.citation_count == len(report.citations)
    assert summary.tools_used == list(report.tools_used)
    assert summary.confidence == report.confidence
    assert summary.evidence_count == len(Blackboard.open(summary.run_id).read("evidence")[0])


def test_a_citation_resolves_to_a_captured_evidence_source_ref() -> None:
    """The benchmark-ready property: the answer's sources are the run's evidence."""
    summary = _execute(lambda run_id, pattern, recorder: _PilotRunner(run_id))
    blackboard = Blackboard.open(summary.run_id)
    report = blackboard.read("final_report")[0]
    evidence = blackboard.read("evidence")[0]

    recorded = {item.source_ref for item in evidence}
    cited = {citation.source_ref for citation in report.citations}
    assert cited <= recorded
    assert len([ref for ref in cited if ref]) >= 1


def test_the_summary_line_shows_no_reason_for_a_clean_run() -> None:
    summary = _execute(lambda run_id, pattern, recorder: _PilotRunner(run_id))

    assert "degraded False" in summary_line(summary)
    assert "[" not in summary_line(summary).split("degraded")[1]


# --- (b) a real loss is degraded, with a reason ----------------------------


def test_a_lost_toolset_degrades_the_run_with_a_reason(monkeypatch) -> None:
    """A toolset that never loaded is a real, named degradation."""
    rows = [
        {
            "server": "web",
            "toolset": "MCPToolset",
            "cause": "ValueError",
            "error": "I/O operation on closed file",
            "required_tools": ["fetch_page"],
            "agent": "web_researcher",
        }
    ]
    monkeypatch.setattr(toolset_health, "failures", lambda run_id, store=None: rows)

    summary = RunSummary(pattern="hierarchical", run_id="run-lost-toolset")
    returned = toolset_health.note_on_summary(summary, "run-lost-toolset")

    assert returned == rows
    assert summary.degraded is True
    assert summary.degraded_reason, "a degradation must name its cause"
    assert "web" in summary.degraded_reason
    assert any("toolset web failed to load" in error for error in summary.stage_errors)

    finish(summary)
    assert summary.degraded is True
    assert "LOST TOOLSETS 1" in summary_line(summary)


def test_a_stage_error_that_lost_work_degrades_the_run() -> None:
    summary = RunSummary(pattern="hierarchical")
    summary.stage_errors.append("analyst: IncompleteOutputException")

    finish(summary)

    assert summary.degraded is True
    assert "analyst: IncompleteOutputException" in summary.degraded_reason


def test_a_budget_stop_degrades_the_run() -> None:
    summary = RunSummary(pattern="hierarchical")
    summary.stage_errors.append("web_researcher: tool_budget")

    finish(summary)

    assert summary.degraded is True
    assert "tool_budget" in summary.degraded_reason


def test_a_forced_fallback_degrades_the_run() -> None:
    from athena.summary import note_ack

    summary = RunSummary(pattern="sequential")
    note_ack(
        summary,
        json.dumps({"ok": True, "role": "critic", "fallback": True}),
    )
    finish(summary)

    assert summary.degraded is True
    assert "critic" in summary.degraded_reason
    assert "fallback" in summary.degraded_reason


def test_a_non_approved_outcome_degrades_the_run_with_the_outcome_as_reason() -> None:
    rejected = RunSummary(pattern="sequential")
    rejected.critic_status = "rejected"
    rejected.approved = False
    set_outcome(rejected, report_present=True, evidence_sufficient=True)
    finish(rejected)

    assert rejected.degraded is True
    assert rejected.degraded_reason == "outcome rejected"


def test_mark_degraded_refuses_to_set_a_flag_without_a_reason() -> None:
    """A health flag nobody can explain is the defect this replaces."""
    summary = RunSummary(pattern="sequential")

    with pytest.raises(ValueError):
        mark_degraded(summary, "   ")
    assert summary.degraded is False


def test_a_reason_left_behind_by_an_older_caller_is_still_filled_in() -> None:
    """A bare ``summary.degraded = True`` still has to explain itself."""
    summary = RunSummary(pattern="sequential")
    summary.degraded = True  # what the old code did, with no reason
    summary.terminal_state = "failure"

    finish(summary)

    assert summary.degraded is True
    assert summary.degraded_reason


def test_recovered_and_lost_errors_are_kept_apart() -> None:
    """The distinction the pilot needed: reported, versus actually lost."""
    summary = RunSummary(pattern="hierarchical")
    summary.stage_errors.extend(["fetch_page: HTTP 403", "critic: RuntimeError"])
    note_recovered(summary, "fetch_page: HTTP 403")

    assert unrecovered_errors(summary) == ["critic: RuntimeError"]

    finish(summary)
    assert summary.degraded is True
    assert "critic: RuntimeError" in summary.degraded_reason
    assert "HTTP 403" not in summary.degraded_reason


def test_a_run_that_lost_its_report_is_degraded_with_a_reason() -> None:
    """No report, no approval: the run is incomplete and says so."""
    summary = RunSummary(pattern="hierarchical")
    set_outcome(summary, report_present=False, evidence_sufficient=False)
    finish(summary)

    assert summary.outcome == "incomplete"
    assert summary.degraded is True
    assert summary.degraded_reason


# --- the pilot summary file, replayed through the real finalizer -----------


def test_the_pilot_summary_shape_no_longer_reports_degraded(tmp_path) -> None:
    """Replay the recorded pilot artifacts and read the flags back.

    The blackboard contents and the event stream are the pilot's; only the model
    calls are absent. The outcome must be approved and *not* degraded.
    """
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(
        Task(question=QUESTION, pattern="hierarchical"), store=store
    )
    set_recorder(Recorder(blackboard.run_id, SQLiteEventStore.from_env()))
    try:
        _record_tool_call(
            blackboard.run_id, agent="web_researcher", tool="web_search",
            args="{'query': 'draco'}",
            ok=True,
        )
        _record_tool_call(
            blackboard.run_id, agent="web_researcher", tool="fetch_page",
            args="{'url': 'https://en.wikipedia.org/wiki/SpaceX_Draco'}",
            ok=False, error="HTTP 403",
        )
        _record_tool_call(
            blackboard.run_id, agent="web_researcher", tool="fetch_page",
            args="{'url': 'https://www.rsc.org/periodic-table/'}",
            ok=True,
        )
        for index in range(6):
            blackboard.append(
                "evidence",
                EvidenceItem(
                    id=f"web-{index}",
                    source_type="web",
                    source_ref=PILOT_URLS[index % len(PILOT_URLS)],
                    content=EVIDENCE_BODY,
                    retrieved_by="web_researcher",
                ),
                author="web_researcher",
            )
        blackboard.write(
            "final_report",
            FinalReport(
                answer="Draco burns MMH/NTO hypergolically at about 300 s of Isp.",
                key_points=["Hypergolic ignition."],
                citations=[
                    Citation(
                        source_type="web",
                        source_ref=url,
                        quote_or_summary="Draco propellant and Isp.",
                    )
                    for url in PILOT_URLS
                ],
                confidence=0.55,
                tools_used=["fetch_page", "web_search"],
                run_id=blackboard.run_id,
            ),
            author="writer",
        )
        blackboard.write(
            "critique", Critique(approved=True, severity="low", issues=[]), author="critic"
        )

        summary = runner_mod._finalize(
            RunSummary(pattern="hierarchical", run_id=blackboard.run_id), blackboard
        )
    finally:
        set_recorder(None)

    assert summary.outcome == "approved"
    assert summary.terminal_state == "success"
    assert summary.degraded is False
    assert summary.degraded_reason == ""
    assert summary.evidence_count == 6
    assert summary.citation_count == 3
    assert summary.tools_used == ["fetch_page", "web_search"]
    assert any("HTTP 403" in error for error in summary.recovered_errors)


def test_the_json_summary_carries_the_reason_and_the_real_citations(tmp_path) -> None:
    summary = _execute(lambda run_id, pattern, recorder: _PilotRunner(run_id))
    target = tmp_path / "pilot.summary.json"
    runner_mod.write_summary_json(summary, str(target))

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["degraded"] is False
    assert payload["degraded_reason"] == ""
    assert payload["citation_count"] == 3
    assert payload["evidence_count"] == 6
