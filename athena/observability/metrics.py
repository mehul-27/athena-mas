"""Per-agent metrics derived from the event log.

Sums over the runs you pass in; ``AgentRow.means`` gives the per-run average.
No pandas: the table is a list of dataclasses.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable

from athena.observability.events import Event

COLUMNS = (
    "stage_ms",
    "model_calls",
    "tokens_in",
    "tokens_out",
    "tool_calls",
    "errors",
    "limiter_wait_ms",
    "a2a_bytes",
    "evidence_items",
)


@dataclass
class AgentRow:
    agent: str
    framework: str = ""
    stage_ms: float = 0.0
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    errors: int = 0
    limiter_wait_ms: float = 0.0
    a2a_bytes: int = 0
    evidence_items: int = 0
    runs: int = 1
    stage_ms_list: list[float] = field(default_factory=list)
    a2a_tokens_in: int = 0
    a2a_tokens_out: int = 0

    def means(self) -> dict[str, float]:
        runs = max(1, self.runs)
        values = asdict(self)
        return {name: float(values[name]) / runs for name in COLUMNS}

    def as_dict(self) -> dict:
        values = asdict(self)
        for key in ("stage_ms_list", "a2a_tokens_in", "a2a_tokens_out"):
            values.pop(key, None)
        return values


def _blank(agent: str) -> AgentRow:
    return AgentRow(agent=agent)


def agent_metrics(run_ids: Iterable[str], events_by_run: dict[str, list[Event]] | None = None,
                  evidence_by_run: dict[str, list[str]] | None = None) -> list[AgentRow]:
    """One row per agent, summed over ``run_ids``.

    ``events_by_run`` lets the caller supply events it already loaded;
    ``evidence_by_run`` maps a run to the ``retrieved_by`` of its evidence.
    """
    rows: dict[str, AgentRow] = {}
    seen_runs = list(run_ids)

    for run_id in seen_runs:
        events = (events_by_run or {}).get(run_id, [])
        touched: set[str] = set()
        for event in events:
            row = rows.setdefault(event.agent, _blank(event.agent))
            row.framework = event.framework or row.framework
            touched.add(event.agent)
            if event.kind == "stage_end":
                row.stage_ms += event.duration_ms or 0.0
                row.stage_ms_list.append(event.duration_ms or 0.0)
            elif event.kind == "model_call":
                row.model_calls += 1
                row.tokens_in += event.tokens_in or 0
                row.tokens_out += event.tokens_out or 0
            elif event.kind == "tool_call":
                row.tool_calls += 1
            elif event.kind == "error":
                row.errors += 1
            elif event.kind == "limiter_wait":
                row.limiter_wait_ms += event.duration_ms or 0.0
            elif event.kind == "a2a_call":
                row.a2a_bytes += int(event.extra.get("request_bytes", 0))
                row.a2a_bytes += int(event.extra.get("response_bytes", 0))
                row.a2a_tokens_in += event.tokens_in or 0
                row.a2a_tokens_out += event.tokens_out or 0

        for agent in (evidence_by_run or {}).get(run_id, []):
            row = rows.setdefault(agent, _blank(agent))
            row.evidence_items += 1

        for agent in touched:
            rows[agent].runs = len(seen_runs)

    for row in rows.values():
        # CrewAI tokens arrive twice: once from the server's own model_call
        # events, once echoed in the A2A ack. Prefer the server's numbers and
        # fall back to the ack when that process could not write (monitoring on
        # the ADK side only).
        if not row.tokens_in and not row.tokens_out:
            row.tokens_in, row.tokens_out = row.a2a_tokens_in, row.a2a_tokens_out

    return sorted(rows.values(), key=lambda row: row.agent)


def format_metrics(rows: list[AgentRow]) -> str:
    """Plain-text table, one line per agent."""
    widths = {"agent": 15, "stage_ms": 9, "calls": 6, "in": 8, "out": 8,
              "tools": 6, "err": 4, "wait_ms": 9, "a2a_b": 8, "evid": 5}
    header = (
        f"{'agent':<{widths['agent']}} {'stage_ms':>{widths['stage_ms']}} "
        f"{'models':>{widths['calls']}} {'tok_in':>{widths['in']}} "
        f"{'tok_out':>{widths['out']}} {'tools':>{widths['tools']}} "
        f"{'err':>{widths['err']}} {'wait_ms':>{widths['wait_ms']}} "
        f"{'a2a_b':>{widths['a2a_b']}} {'evid':>{widths['evid']}}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row.agent:<{widths['agent']}} {row.stage_ms:>{widths['stage_ms']}.0f} "
            f"{row.model_calls:>{widths['calls']}} {row.tokens_in:>{widths['in']}} "
            f"{row.tokens_out:>{widths['out']}} {row.tool_calls:>{widths['tools']}} "
            f"{row.errors:>{widths['err']}} {row.limiter_wait_ms:>{widths['wait_ms']}.0f} "
            f"{row.a2a_bytes:>{widths['a2a_b']}} {row.evidence_items:>{widths['evid']}}"
        )
    return "\n".join(lines)
