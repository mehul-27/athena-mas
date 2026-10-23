"""Per-run measurements and the comparison table.

Deliberately in-memory: Step 6 will persist these, this step only measures.
"""

from __future__ import annotations

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
