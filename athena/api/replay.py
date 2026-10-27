"""Recorded-run bundles served by the demo API."""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_DIR = Path("data/demo/replay")
REQUIRED = ("run_id", "summary", "events", "blackboard", "metrics")


class ReplayError(RuntimeError):
    """A bundle is missing or malformed."""


def replay_dir() -> Path:
    import os

    return Path(os.getenv("ATHENA_REPLAY_DIR") or DEFAULT_DIR)


def names() -> list[str]:
    directory = replay_dir()
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.json"))


def validate(bundle: dict) -> dict:
    missing = [key for key in REQUIRED if key not in bundle]
    if missing:
        raise ReplayError(f"bundle missing {', '.join(missing)}")
    if not isinstance(bundle["events"], list):
        raise ReplayError("bundle events must be a list")
    return bundle


def load(name: str) -> dict:
    """One bundle by name, validated."""
    safe = Path(name).name.removesuffix(".json")
    path = replay_dir() / f"{safe}.json"
    if not path.exists():
        raise ReplayError(f"no replay bundle named {safe!r}")
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except ValueError as exc:
        raise ReplayError(f"bundle {safe!r} is not valid JSON") from exc


def index() -> list[dict]:
    """A small catalogue for the UI: name, pattern, question, verdict."""
    entries = []
    for name in names():
        try:
            bundle = load(name)
        except ReplayError:
            continue
        summary = bundle.get("summary") or {}
        entries.append({
            "name": name,
            "run_id": bundle["run_id"],
            "pattern": summary.get("pattern", "?"),
            "degraded": bool(summary.get("degraded")),
            "approved": summary.get("approved"),
            "events": len(bundle["events"]),
        })
    return entries
