"""Recorded run bundles: everything the demo needs, with no secrets.

    python -m athena.demo.export --run <id> --name sequential

A bundle is one JSON file under ``data/demo/replay``: the summary, every event
(redacted), the blackboard snapshot, its history, the metrics rows and the
heatmap PNG as base64. The API serves it back when there is no provider.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from athena.blackboard import Blackboard
from athena.observability.heatmap import render_heatmap
from athena.observability.metrics import agent_metrics
from athena.observability.redact import redact
from athena.observability.store import SQLiteEventStore

SECTIONS = ("task", "plan", "analysis", "draft", "critique", "final_report")
DEFAULT_OUT = Path("data/demo/replay")


def _clean(value):
    """Redact strings and drop absolute filesystem paths."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        text = redact(value)
        # Bundle paths are relative to the repo; never leak a home directory.
        return text.replace(str(Path.home()), "~")
    return value


def _section(blackboard: Blackboard, name: str):
    found = blackboard.read(name)
    if not found:
        return None
    value, version = found
    return {"value": _clean(value.model_dump(mode="json")), "version": version}


def _summary_event(events) -> dict:
    for event in reversed(events):
        if event.payload_summary and event.payload_summary.startswith("run_summary"):
            return dict(event.extra)
    return {}


def build_bundle(run_id: str) -> dict:
    store = SQLiteEventStore()
    events = store.events(run_id)
    if not events:
        raise SystemExit(f"no events for run {run_id} in {store.path}")

    blackboard = Blackboard.open(run_id)
    history = [
        {"seq": entry["seq"], "author": entry["author"], "section": entry["section"],
         "operation": entry["operation"], "version": entry["version"]}
        for entry in blackboard.history()
    ]
    evidence = blackboard.read("evidence")
    items = [
        {"id": item.id, "source_ref": item.source_ref, "score": item.score,
         "retrieved_by": item.retrieved_by, "text": item.content[:500]}
        for item in (evidence[0] if evidence else [])
    ]

    rows = agent_metrics([run_id], {run_id: events}, {})
    heatmap = render_heatmap(rows, Path(".run/replay-heatmap.png"), f"Athena {run_id[:8]}")
    png = base64.b64encode(heatmap.read_bytes()).decode("ascii")

    return _clean({
        "run_id": run_id,
        "summary": _summary_event(events),
        "events": [json.loads(event.to_json()) for event in events],
        "blackboard": {name: _section(blackboard, name) for name in SECTIONS},
        "evidence": items,
        "history": history,
        "metrics": [row.as_dict() for row in rows],
        "heatmap_png": png,
    })


def write_bundle(bundle: dict, name: str, out: Path = DEFAULT_OUT) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{name}.json"
    target.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="athena.demo.export")
    parser.add_argument("--run", required=True, help="run id to export")
    parser.add_argument("--name", required=True, help="bundle name (file stem)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    args = parser.parse_args(argv)

    target = write_bundle(build_bundle(args.run), args.name, Path(args.out))
    print(f"{target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
