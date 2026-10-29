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
    tool_calls: int = 0
    revision_rounds: int = 0
    confidence: float | None = None
    approved: bool | None = None
    severity: str | None = None
    critic_status: str = "missing"
    critique_reason: str = ""
    evidence_sufficient: bool | None = None
    outcome: str = "incomplete"
    stage_errors: list[str] = field(default_factory=list)
    degraded: bool = False
    failed: bool = False
    estimated: bool = False

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
        summary.degraded = True
    if body.get("estimated"):
        summary.estimated = True
    return None


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


def finish(summary: RunSummary) -> RunSummary:
    """A run with any failed stage is degraded, whatever the end state looks like."""
    summary.degraded = summary.degraded or bool(summary.stage_errors)
    return summary


def set_outcome(
    summary: RunSummary, report_present: bool, evidence_sufficient: bool
) -> RunSummary:
    """Classify a normally terminated run without calling it a success by default."""
    summary.evidence_sufficient = evidence_sufficient
    if summary.failed:
        outcome = "failed"
    elif not report_present:
        outcome = "incomplete"
    elif summary.critic_status in {"failed", "missing"}:
        outcome = "incomplete"
    elif summary.approved is False:
        outcome = "rejected"
    elif summary.approved is True and evidence_sufficient:
        outcome = "approved"
    else:
        outcome = "incomplete"
    summary.outcome = outcome
    if outcome != "approved":
        summary.degraded = True
    return summary

def summary_line(summary: RunSummary) -> str:
    """One line for the runner: cost, size and health of the run."""
    errors = f" ({', '.join(summary.stage_errors)})" if summary.stage_errors else ""
    return (
        f"summary: {summary.pattern} | wall {summary.wall_s:.0f}s | "
        f"ADK tokens {summary.adk_tokens} | "
        f"A2A {summary.a2a_bytes_out}B out / {summary.a2a_bytes_in}B in | "
        f"evidence {summary.evidence_count} | tools {summary.tool_calls} | "
        f"revisions {summary.revision_rounds} | degraded {summary.degraded}{errors} | "
        f"critic {_verdict(summary)} | "
        + ("cost basis: partly estimated" if summary.estimated else "cost basis: measured")
    )

def _verdict(summary: RunSummary) -> str:
    """'approved (low)' / 'rejected (high)' / 'unknown' for the summary line."""
    if summary.approved is None:
        return "unknown"
    verdict = "approved" if summary.approved else "rejected"
    return f"{verdict} ({summary.severity})" if summary.severity else verdict