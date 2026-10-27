"""Re-record the demo bundles: four patterns, one after another, then export.

    python scripts/record_demo.py                 # first curated question
    python scripts/record_demo.py --overwrite     # replace the shipped bundles
    python scripts/record_demo.py --keep-bad      # accept degraded runs

This makes four real runs against the configured provider (roughly 4-5 minutes)
and refuses to ship a run that is degraded or below 0.5 confidence, so a broken
run never becomes demo material.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

PATTERNS = ("sequential", "parallel", "hierarchical", "p2p")
QUESTIONS = REPO / "data" / "demo" / "questions.json"
RUN_DIR = REPO / ".run" / "runs"
REPLAY = REPO / "data" / "demo" / "replay"


def run_cli(argv: list[str]) -> int:
    """One pipeline run. Tests replace this."""
    return subprocess.call(argv, cwd=str(REPO))


def first_question() -> str:
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    return questions[0]["text"]


def run_pattern(pattern: str, question: str) -> dict:
    run_id = str(uuid.uuid4())
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = RUN_DIR / f"{run_id}.summary.json"
    started = time.perf_counter()
    code = run_cli([
        sys.executable, "-m", "athena.run", question,
        "--pattern", pattern, "--run-id", run_id, "--json-summary", str(summary_path),
    ])
    wall = time.perf_counter() - started
    summary = {}
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    return {"pattern": pattern, "run_id": run_id, "exit": code,
            "wall": wall, "summary": summary}


def acceptable(run: dict, keep_bad: bool) -> tuple[bool, str]:
    if run["exit"] != 0:
        return False, f"exit code {run['exit']}"
    summary = run["summary"]
    if not summary:
        return False, "no summary written"
    if keep_bad:
        return True, ""
    if summary.get("degraded"):
        return False, "degraded"
    confidence = summary.get("confidence")
    if confidence is not None and confidence < 0.5:
        return False, f"confidence {confidence}"
    return True, ""


def export(run: dict, overwrite: bool) -> Path:
    from athena.demo.export import build_bundle, write_bundle

    name = run["pattern"] if overwrite else f"{run['pattern']}-new"
    return write_bundle(build_bundle(run["run_id"]), name, REPLAY)


def status_line(run: dict) -> str:
    summary = run["summary"]
    verdict = summary.get("approved")
    word = "approved" if verdict is True else "rejected" if verdict is False else "no verdict"
    return (
        f"{run['pattern']:<13} {run['wall']:6.1f}s  degraded={str(bool(summary.get('degraded'))):<5} "
        f"{word:<12} confidence={summary.get('confidence')}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="record_demo")
    parser.add_argument("--question", default="", help="default: first curated demo question")
    parser.add_argument("--overwrite", action="store_true", help="replace the shipped bundles")
    parser.add_argument("--keep-bad", action="store_true", help="accept degraded runs")
    args = parser.parse_args(argv)

    import demo as launcher  # scripts/demo.py

    if not launcher.provider_ready():
        print("No provider configured (LLM_API_KEY / LLM_MODEL). "
              "Set them, or use the existing bundles with the replay mode.")
        return 2
    if launcher.start_crew_server() is None:
        print("The CrewAI server did not start; nothing recorded.")
        return 2

    question = args.question or first_question()
    print(f"question: {question[:100]}")
    kept: list[Path] = []
    failures = 0
    try:
        for pattern in PATTERNS:
            run = run_pattern(pattern, question)
            print(status_line(run))
            ok, why = acceptable(run, args.keep_bad)
            if not ok:
                failures += 1
                print(f"  not exported: {why}")
                continue
            kept.append(export(run, args.overwrite))
    finally:
        launcher.stop_crew_server()

    print("\nexported:")
    for path in kept:
        print(f"  {path} ({path.stat().st_size} bytes)")
    if failures:
        print(f"\n{failures} pattern(s) were not exported. Re-run, or pass --keep-bad.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
