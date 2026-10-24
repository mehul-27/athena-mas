"""Read the event log: `python -m athena.observability.report --run <id> | --last N`.

Prints the event table per run and the per-agent metrics, and writes the heatmap
PNG plus its CSV to ``--out`` (default ``.run/report``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from athena.observability.events import Event
from athena.observability.heatmap import render_heatmap
from athena.observability.metrics import agent_metrics, format_metrics
from athena.observability.store import SQLiteEventStore


def event_table(events: list[Event], limit: int = 400) -> str:
    header = f"{'seq':>4}  {'kind':<17} {'agent':<15} {'ms':>8} {'tok_in':>7} {'tok_out':>8}  detail"
    lines = [header, "-" * len(header)]
    for event in events[:limit]:
        detail = event.payload_summary or event.error_message or ""
        lines.append(
            f"{event.seq:>4}  {event.kind:<17} {event.agent:<15} "
            f"{(event.duration_ms or 0):>8.0f} "
            f"{(event.tokens_in if event.tokens_in is not None else '-'):>7} "
            f"{(event.tokens_out if event.tokens_out is not None else '-'):>8}  {detail[:60]}"
        )
    return "\n".join(lines)


def pattern_of(events: list[Event]) -> str:
    for event in events:
        if event.payload_summary and event.payload_summary.startswith("run_summary"):
            return str(event.extra.get("pattern", "?"))
    return "?"


def evidence_authors(events: list[Event]) -> list[str]:
    return [
        event.agent
        for event in events
        if event.kind == "blackboard_write" and event.extra.get("section") == "evidence"
    ]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="athena.observability.report")
    parser.add_argument("--run", help="one run id")
    parser.add_argument("--last", type=int, help="the N most recent runs")
    parser.add_argument("--out", default=".run/report", help="where to write the heatmap")
    args = parser.parse_args(argv)

    store = SQLiteEventStore.from_env()
    if args.run:
        run_ids = [args.run]
    elif args.last:
        run_ids = store.runs()[-args.last:]
    else:
        raise SystemExit("usage: python -m athena.observability.report --run <id> | --last N")
    if not run_ids:
        raise SystemExit("no runs in the event log yet")

    events_by_run = {run_id: store.events(run_id) for run_id in run_ids}
    for run_id in run_ids:
        print(f"\n=== run {run_id} ({pattern_of(events_by_run[run_id])}) ===")
        print(event_table(events_by_run[run_id]))

    rows = agent_metrics(
        run_ids,
        events_by_run,
        {run_id: evidence_authors(events) for run_id, events in events_by_run.items()},
    )
    print("\nper-agent metrics (sums over the selected runs):")
    print(format_metrics(rows))

    patterns = sorted({pattern_of(events_by_run[run_id]) for run_id in run_ids})
    title = f"Athena {', '.join(patterns)} - {len(run_ids)} run(s)"
    out = Path(args.out)
    png = render_heatmap(rows, out / "heatmap.png", title)
    print(f"\nheatmap: {png} ({png.stat().st_size} bytes)")
    print(f"table:   {png.with_suffix('.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
