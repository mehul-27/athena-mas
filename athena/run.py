"""Run the Athena pipeline: ``python -m athena.run "<question>" [--pattern NAME]``.

``--eval-id N`` pulls question N from the NASA evaluation set; ``--compare`` runs
the same question under every pattern and prints one table.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from dataclasses import asdict
import sys
import time

from google.adk.agents.run_config import RunConfig
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.runners import InMemoryRunner
from google.genai import types

from athena.agents import a2a_client
from athena.observability import Recorder, set_recorder
from athena.observability.adk_plugin import MonitoringPlugin
from athena.observability.hooks import install_hooks
from athena.agents.a2a_stage import DEFAULT_CARD_URL
from athena.agents.supervisor import build_supervisor
from athena.config.caps import cap_message, llm_call_cap
from athena.config.llm import configure_process
from athena.blackboard import Blackboard, Task
from athena.patterns import VALID_PATTERNS, UnknownPatternError
from athena.run_report import ack_warning, parts_of, print_report, text_of, tool_lines
from athena.summary import (
    RunSummary, finish, format_table, note_ack, note_critique, set_outcome, summary_line,
)
from athena.agents.sufficiency import assess
from athena.tools import settings

# ADK derives the app name from the root agent's package ("athena/agents"); use
# the same name so it does not warn about a mismatch.
APP_NAME = "agents"
USER_ID = "student"


def question_from_args(args: argparse.Namespace) -> str:
    if args.eval_id is not None:
        path = settings.eval_dir() / "nasa_rag_40_questions.csv"
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row["id"] == str(args.eval_id):
                    return row["question"]
        raise SystemExit(f"no question with id {args.eval_id} in {path}")
    if not args.question:
        raise SystemExit('usage: python -m athena.run "<question>" [--pattern NAME] | --eval-id N')
    return args.question


def server_is_up(card_url: str = DEFAULT_CARD_URL) -> bool:
    import httpx

    try:
        return httpx.get(card_url, timeout=3.0).status_code == 200
    except Exception:  # noqa: BLE001 - any failure means "not up"
        return False


def default_runner(run_id: str, pattern: str, recorder) -> "InMemoryRunner":
    """The real ADK runner. Tests inject a stub factory instead."""
    return InMemoryRunner(
        agent=build_supervisor(run_id, pattern=pattern),
        app_name=APP_NAME,
        plugins=[MonitoringPlugin(recorder)],
    )


async def _execute(
    question: str,
    pattern: str,
    verbose: bool = True,
    runner_factory=None,
    run_id: str | None = None,
) -> RunSummary:
    blackboard = Blackboard.create(Task(question=question, pattern=pattern, run_id=run_id or ""))
    recorder = Recorder.for_run(blackboard.run_id)
    set_recorder(recorder)
    install_hooks()

    build_runner = runner_factory or default_runner
    runner = build_runner(blackboard.run_id, pattern, recorder)
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)

    summary = RunSummary(pattern=pattern, run_id=blackboard.run_id)
    a2a_client.reset_stats()
    started = time.perf_counter()

    if verbose:
        print(f"\nrun_id: {blackboard.run_id}\nquestion: {question}\npattern: {pattern}\nstages:")

    # A hard ceiling on model calls: a model that keeps re-calling a refused
    # tool would otherwise loop until the provider quota runs out.
    cap = llm_call_cap(pattern)
    run_config = RunConfig(max_llm_calls=cap)

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=question)]),
        run_config=run_config,
    ):
        metadata = event.custom_metadata or {}
        if "stage" in metadata:
            summary.stage_ms[metadata["stage"]] = metadata["stage_ms"]
            if verbose:
                print(f"  {metadata['stage']:<13} {metadata['stage_ms'] / 1000:6.1f}s")
        if "revision_rounds" in metadata:
            summary.revision_rounds = metadata["revision_rounds"]
        if verbose:
            for line in tool_lines(event):
                print(f"    {line}")
            text = text_of(event)
            if "a2a_payload" in metadata:
                print(f"    [a2a request] {metadata['a2a_payload_bytes']}B {metadata['a2a_payload']}")
            if event.author in {"analyst", "writer", "critic"} and text.startswith("{"):
                note_ack(summary, text)
                print(f"    [a2a] {text[:160]}")
                if warning := ack_warning(text):
                    print(warning)

        summary.tool_calls += sum(1 for part in parts_of(event) if part.function_call)
        usage = getattr(event, "usage_metadata", None)
        if usage is not None:
            summary.adk_tokens += int(getattr(usage, "prompt_token_count", 0) or 0)

    summary.wall_s = time.perf_counter() - started
    traffic = a2a_client.stats()
    summary.a2a_bytes_out = traffic["out"]
    summary.a2a_bytes_in = traffic["in"]

    evidence = blackboard.read("evidence")
    summary.evidence_count = len(evidence[0]) if evidence else 0
    task = blackboard.read("task")
    assessment = assess(
        task[0].question if task else "",
        evidence[0] if evidence else [],
    )
    summary.evidence_sufficient = assessment.ok
    report = blackboard.read("final_report")
    summary.confidence = report[0].confidence if report else None
    critique = blackboard.read("critique")
    note_critique(summary, critique[0] if critique else None)
    finish(summary)
    set_outcome(summary, report is not None, assessment.ok)

    # Persist the summary as one event so Step 9 can read everything in one place.
    recorder.emit(
        "stage_end",
        "supervisor",
        "adk",
        payload_summary=f"run_summary {pattern}",
        extra=asdict(summary),
    )
    set_recorder(None)

    if verbose:
        print_report(blackboard, summary.wall_s)
    return summary


async def run_one(question: str, pattern: str, run_id: str = "") -> RunSummary:
    configure_process()  # export the endpoint and arm the guard
    try:
        summary = await _execute(question, pattern, verbose=True, run_id=run_id or None)
    except LlmCallsLimitExceededError:
        return _cap_reached(pattern)
    print("\n" + summary_line(summary))
    return summary


def _cap_reached(pattern: str) -> RunSummary:
    """A capped run is a failed run: say so plainly, no traceback."""
    cap = llm_call_cap(pattern)
    print("\n" + cap_message(pattern, cap), file=sys.stderr)
    recorder = Recorder.for_run(f"cap-{pattern}")
    recorder.emit(
        "error", "supervisor", "adk", ok=False,
        error_type="LlmCallsLimitExceededError",
        error_message=f"cap {cap} reached for pattern {pattern}",
    )
    summary = RunSummary(pattern=pattern, run_id=f"cap-{pattern}")
    summary.failed = True
    summary.degraded = True
    return summary


async def run_compare(question: str) -> list[RunSummary]:
    summaries = []
    for pattern in VALID_PATTERNS:
        summary = await _execute(question, pattern, verbose=False)
        print(f"{pattern:<13} done in {summary.wall_s:.0f}s")
        print(summary.stage_line())
        summaries.append(summary)
    print("\n" + format_table(summaries))
    return summaries


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="athena.run")
    parser.add_argument("question", nargs="?", help="the research question")
    parser.add_argument("--eval-id", type=int, help="question id from the NASA eval CSV")
    parser.add_argument("--pattern", default="sequential", help=f"one of {list(VALID_PATTERNS)}")
    parser.add_argument("--compare", action="store_true", help="run every pattern on this question")
    # Used by the demo API: it needs the run id before the run starts.
    parser.add_argument("--run-id", default="", help="use this run id instead of a new one")
    parser.add_argument("--json-summary", default="", help="write the RunSummary as JSON here")
    args = parser.parse_args(argv)

    if args.pattern not in VALID_PATTERNS:
        raise SystemExit(f"unknown pattern {args.pattern!r}; choose one of {list(VALID_PATTERNS)}")

    if not server_is_up():
        print(
            "The CrewAI A2A server is not answering on 127.0.0.1:9001.\n"
            "Start it with:  python scripts/dev_up.py",
            file=sys.stderr,
        )
        return 1

    question = question_from_args(args)
    try:
        if args.compare:
            asyncio.run(run_compare(question))
        else:
            summary = asyncio.run(run_one(question, args.pattern, run_id=args.run_id))
            if args.json_summary:
                write_summary_json(summary, args.json_summary)
            if summary.failed:
                return 2
    except UnknownPatternError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def write_summary_json(summary: RunSummary, path: str) -> None:
    """Write the RunSummary so the demo API can read it without the blackboard."""
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(summary), indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())