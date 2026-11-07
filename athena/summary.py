"""Per-run measurements and the comparison table.

Deliberately in-memory: Step 6 will persist these, this step only measures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

TABLE_COLUMNS = (
    ("pattern", 12),
    ("wall s", 7),
    ("ADK tok", 8),
    ("A2A out", 8),
    ("A2A in", 7),
    ("evid", 5),
    ("tools", 6),
    ("rev", 4),
    ("conf", 5),
    ("approved", 8),
    ("severity", 9),
    ("degraded", 9),
)


@dataclass
class RunSummary:
    """What one run cost and produced."""

    pattern: str
    run_id: str = ""
    wall_s: float = 0.0
    stage_ms: dict[str, float] = field(default_factory=dict)
    adk_tokens: int = 0
    a2a_bytes_out: int = 0
    a2a_bytes_in: int = 0
    evidence_count: int = 0
    # Citations and tools the shipped report actually carries. Both are read
    # from the blackboard, never from a model's self-report.
    citation_count: int = 0
    tools_used: list[str] = field(default_factory=list)
    tool_calls: int = 0
    revision_rounds: int = 0
    recovery_rounds: int = 0
    confidence: float | None = None
    approved: bool | None = None
    severity: str | None = None
    critic_status: str = "missing"
    critique_reason: str = ""
    evidence_sufficient: bool | None = None
    outcome: str = "incomplete"
    stage_errors: list[str] = field(default_factory=list)
    degraded: bool = False
    # Why the run is degraded. Non-empty whenever ``degraded`` is True: a health
    # flag with no reason can neither be acted on nor checked.
    degraded_reason: str = ""
    # Stage errors a stage absorbed and carried on from. A single tool call that
    # failed while the stage still gathered evidence belongs here: it is a real
    # event worth reporting, but it cost the run no work, so it must not mark an
    # otherwise approved run degraded (the pilot's HTTP 403 fetch did exactly
    # that). Work that was actually lost is never listed here.
    recovered_errors: list[str] = field(default_factory=list)
    failed: bool = False
    estimated: bool = False
    # MCP toolsets that failed to load. A lost toolset is a lost capability: the
    # run cannot have gathered evidence with a tool that never started, so its
    # presence always marks the run degraded.
    toolset_failures: list[dict] = field(default_factory=list)
    tools_enabled: list[str] = field(default_factory=list)
    tools_planned: list[str] = field(default_factory=list)
    tools_invoked: list[str] = field(default_factory=list)
    tools_succeeded: list[str] = field(default_factory=list)
    tools_failed: list[str] = field(default_factory=list)
    tools_usable_evidence: list[str] = field(default_factory=list)
    tools_skipped: list[str] = field(default_factory=list)
    unmet_requirements: list[str] = field(default_factory=list)
    evidence_limitations: list[str] = field(default_factory=list)
    subject_specific: bool = False
    general_background: bool = False
    answer_as_asked: bool = False
    qualified_possible: bool = False
    supported_aspects: list[str] = field(default_factory=list)
    missing_aspects: list[str] = field(default_factory=list)
    missing_subjects: list[str] = field(default_factory=list)
    # Requested facts the evidence does not establish, and unresolved conflicts.
    missing_requirements: list[str] = field(default_factory=list)
    conflict_notes: list[str] = field(default_factory=list)
    evidence_quality: str = "none"
    confidence_ceiling: float | None = None
    confidence_notes: list[str] = field(default_factory=list)
    # Claim-level validation of the shipped answer.
    claim_checks: int = 0
    claim_issues: list[str] = field(default_factory=list)
    # Exactly one terminal state per run, and the reason for it.
    terminal_state: str = ""
    terminal_reason: str = ""
    recovery_reason: str = ""
    recovery_skipped_reason: str = ""

    def row(self) -> tuple[str, ...]:
        return (
            self.pattern,
            f"{self.wall_s:.0f}",
            str(self.adk_tokens),
            str(self.a2a_bytes_out),
            str(self.a2a_bytes_in),
            str(self.evidence_count),
            str(self.tool_calls),
            str(self.revision_rounds),
            "-" if self.confidence is None else f"{self.confidence:.2f}",
            "-" if self.approved is None else str(self.approved).lower(),
            self.severity or "-",
            "yes" if self.degraded else "no",
        )

    def stage_line(self) -> str:
        parts = [f"{name} {ms / 1000:.1f}s" for name, ms in self.stage_ms.items()]
        return "  stages: " + " | ".join(parts) if parts else "  stages: (none)"


def format_table(summaries: list[RunSummary]) -> str:
    """A fixed-width table, one row per run."""
    header = " | ".join(name.ljust(width) for name, width in TABLE_COLUMNS)
    divider = "-+-".join("-" * width for _, width in TABLE_COLUMNS)
    lines = [header, divider]
    for summary in summaries:
        lines.append(
            " | ".join(
                value.ljust(width) for value, (_, width) in zip(summary.row(), TABLE_COLUMNS)
            )
        )
    return "\n".join(lines)

def note_ack(summary: RunSummary, text: str) -> dict | None:
    """Fold a CrewAI acknowledgement into the run's health flags.

    Returns the body when the role failed, so the runner can print the warning.
    """
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    if not body.get("ok", True):
        summary.stage_errors.append(
            f"{body.get('role', '?')}: {body.get('error_type', 'Error')}"
        )
        return body
    if body.get("fallback"):
        # A role that answered from a forced fallback did not do the work it was
        # asked for, so this one really does degrade the run.
        mark_degraded(summary, f"{body.get('role', 'role')}: forced fallback answer")
    if body.get("estimated"):
        summary.estimated = True
    return None


# --- degradation: a flag is only ever set together with its reason ----------

MAX_DEGRADED_REASON = 400


def mark_degraded(summary: RunSummary, reason: str) -> RunSummary:
    """Mark the run degraded and record *why* in the same breath.

    Every degradation is a lost capability, a lost stage, a budget stop or a
    forced fallback: something the benchmark must be able to name. Calling this
    with an empty reason is a programming error, not a way to set a flag.
    """
    text = " ".join(str(reason or "").split())[:MAX_DEGRADED_REASON]
    if not text:
        raise ValueError("a degraded run must record a non-empty reason")
    summary.degraded = True
    if text not in summary.degraded_reason:
        summary.degraded_reason = (
            f"{summary.degraded_reason} | {text}" if summary.degraded_reason else text
        )[:MAX_DEGRADED_REASON]
    return summary


def note_recovered(summary: RunSummary, detail: str) -> RunSummary:
    """Record a failure a stage absorbed, and count it as *not* a degradation.

    A tool call that failed once while the stage went on to gather evidence is
    reported in ``stage_errors`` for visibility, but it lost no work. Listing it
    in ``recovered_errors`` is what keeps an approved run with real citations
    from being reported degraded because one fetch returned HTTP 403.
    """
    text = str(detail or "").strip()[:MAX_DEGRADED_REASON]
    if not text:
        return summary
    if text not in summary.recovered_errors:
        summary.recovered_errors.append(text)
    return summary


def unrecovered_errors(summary: RunSummary) -> list[str]:
    """Stage errors that cost the run something, in the order they happened."""
    recovered = set(summary.recovered_errors or [])
    return [
        error for error in (summary.stage_errors or [])
        if str(error).strip() and str(error).strip() not in recovered
    ]


def critique_details(critique, stage_errors) -> tuple[str, str]:
    """Return (status, reason) without ever turning a missing critique into approval."""
    if critique is None:
        failed = any(error.startswith("critic:") for error in (stage_errors or []))
        if failed:
            return "failed", "critic failed before producing a critique: " + ", ".join(stage_errors)
        return "missing", "no critique recorded; this is not an approval"
    status = "approved" if critique.approved else "rejected"
    issues = [str(issue).strip() for issue in (critique.issues or []) if str(issue).strip()]
    reason = (
        " | ".join(issues)
        if issues
        else ("approved with no issues" if critique.approved else "rejected without a reason")
    )
    return status, reason


def note_critique(summary: "RunSummary", critique) -> "RunSummary":
    """Record the critic verdict, or distinguish a missing/failed critique."""
    if critique is None:
        summary.critic_status, summary.critique_reason = critique_details(
            None, summary.stage_errors
        )
        return summary

    summary.approved = critique.approved
    summary.severity = critique.severity
    summary.critic_status, summary.critique_reason = critique_details(critique, [])
    return summary


TERMINAL_STATES = ("success", "failure", "cancelled", "timeout")


def set_terminal(summary: RunSummary, state: str, reason: str = "") -> str:
    """Stamp the run's single terminal state; the first state wins.

    A run must end in exactly one of ``TERMINAL_STATES``. Calling this twice
    therefore never overwrites the state a run already reached: a cancelled run
    that later reports "success" is still cancelled, and the discrepancy is
    recorded in ``terminal_reason``.
    """
    from athena.agents.lifecycle import normalise_terminal

    state = normalise_terminal(state)
    if summary.terminal_state:
        if state != summary.terminal_state and reason:
            note = f"{summary.terminal_state} (ignored {state}: {reason})"
            if note != summary.terminal_reason:
                summary.terminal_reason = note[:400]
        return summary.terminal_state
    summary.terminal_state = state
    if reason:
        summary.terminal_reason = reason[:400]
    return state


def finish(summary: RunSummary) -> RunSummary:
    """Close out the health flags, then stamp the terminal state.

    Degraded means *something actually degraded this run*: a lost toolset, a
    stage error that cost work, a budget stop, a forced fallback, or a run that
    did not end approved. The check below therefore reads only unrecovered
    errors: a single failed tool call that the stage absorbed is reported, but it
    is not a degradation, and the pilot's approved run with three real citations
    must not be labelled one.
    """
    for error in unrecovered_errors(summary):
        mark_degraded(summary, error)
    if summary.toolset_failures:
        mark_degraded(
            summary,
            "lost toolset(s): "
            + ", ".join(
                str(row.get("server") or row.get("toolset") or "?")
                for row in summary.toolset_failures
            ),
        )
    if summary.degraded and not summary.degraded_reason:
        # A flag set before this module owned the reason (an older summary, a
        # caller that only flipped the boolean) still has to explain itself.
        mark_degraded(summary, _fallback_reason(summary))
    if not summary.terminal_state:
        set_terminal(
            summary,
            "success" if summary.outcome == "approved" else "failure",
            summary.terminal_reason or f"outcome {summary.outcome}",
        )
    return summary


def _fallback_reason(summary: RunSummary) -> str:
    """A reason for a degraded flag whose cause was never recorded."""
    if summary.failed:
        return "the run failed"
    if summary.terminal_state and summary.terminal_state != "success":
        return f"terminal state {summary.terminal_state}"
    if summary.outcome and summary.outcome != "approved":
        return f"outcome {summary.outcome}"
    return "degraded by an unrecorded cause"


def set_outcome(
    summary: RunSummary, report_present: bool, evidence_sufficient: bool
) -> RunSummary:
    """Classify a normally terminated run without calling it a success by default.

    Only a recorded approval counts as one, and it still needs enough evidence.
    A missing or failed critique never sets ``approved``, so it falls through to
    ``incomplete``; a recorded rejection stays a rejection. The verdict is
    authoritative for approval, so a stale default ``critic_status`` cannot
    demote a run that already carries an explicit approval.
    """
    summary.evidence_sufficient = evidence_sufficient
    if summary.failed:
        outcome = "failed"
    elif not report_present:
        outcome = "incomplete"
    elif summary.approved is True:
        outcome = "approved" if evidence_sufficient else "incomplete"
    elif summary.approved is False:
        outcome = "rejected"
    else:
        # No approval was recorded: nothing here is a success.
        outcome = "incomplete"
    summary.outcome = outcome
    if outcome != "approved":
        mark_degraded(summary, f"outcome {outcome}")
    set_terminal(
        summary,
        "success" if outcome == "approved" else "failure",
        summary.terminal_reason or f"outcome {outcome}",
    )
    return summary

def evidence_on(blackboard) -> list:
    """The evidence items actually recorded on a run's blackboard.

    The blackboard is the authoritative store: every researcher append lands
    there, whoever made it (an ADK worker, a CrewAI role, a p2p audit write).
    A counter derived anywhere else can be stale or never incremented at all,
    which is how a run with three recorded web sources reported ``0``.
    """
    try:
        found = blackboard.read("evidence")
    except Exception:  # noqa: BLE001 - a status read must not break a run
        return []
    return list(found[0]) if found else []


def count_evidence(blackboard) -> int:
    """How many evidence items this run really recorded."""
    return len(evidence_on(blackboard))


def count_citations(blackboard) -> int:
    """How many citations the shipped report really carries."""
    try:
        found = blackboard.read("final_report")
    except Exception:  # noqa: BLE001 - a status read must not break a run
        return 0
    if not found:
        return 0
    return len(getattr(found[0], "citations", None) or [])


def tools_used_on(blackboard) -> list[str]:
    """The tools the shipped report really used, as sanitization recorded them."""
    try:
        found = blackboard.read("final_report")
    except Exception:  # noqa: BLE001 - a status read must not break a run
        return []
    if not found:
        return []
    return [str(tool) for tool in getattr(found[0], "tools_used", None) or []]


def apply_authoritative_counts(summary: "RunSummary", blackboard) -> "RunSummary":
    """Overwrite the derived counters with what the blackboard actually holds.

    Called after the run and before the summary is emitted or written, so the
    numbers a benchmark reads describe the artifacts that exist. It is additive:
    a section that was never written leaves its counter at the honest zero.
    """
    summary.evidence_count = count_evidence(blackboard)
    summary.citation_count = count_citations(blackboard)
    summary.tools_used = tools_used_on(blackboard)
    return summary


def summary_line(summary: RunSummary) -> str:
    """One line for the runner: cost, size and health of the run."""
    errors = f" ({', '.join(summary.stage_errors)})" if summary.stage_errors else ""
    terminal = summary.terminal_state or "unset"
    lost = (
        f" | LOST TOOLSETS {len(summary.toolset_failures)}"
        if summary.toolset_failures
        else ""
    )
    why = f" [{summary.degraded_reason}]" if summary.degraded and summary.degraded_reason else ""
    return (
        f"summary: {summary.pattern} | terminal {terminal} | wall {summary.wall_s:.0f}s | "
        f"ADK tokens {summary.adk_tokens} | "
        f"A2A {summary.a2a_bytes_out}B out / {summary.a2a_bytes_in}B in | "
        f"evidence {summary.evidence_count} | tools {summary.tool_calls} | "
        f"revisions {summary.revision_rounds} | recoveries {summary.recovery_rounds} | "
        f"degraded {summary.degraded}{why}{lost}{errors} | "
        f"critic {_verdict(summary)} | "
        + ("cost basis: partly estimated" if summary.estimated else "cost basis: measured")
    )

def _verdict(summary: RunSummary) -> str:
    """'approved (low)' / 'rejected (high)' / 'unknown' for the summary line."""
    if summary.approved is None:
        return "unknown"
    verdict = "approved" if summary.approved else "rejected"
    return f"{verdict} ({summary.severity})" if summary.severity else verdict