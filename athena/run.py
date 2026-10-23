"""Run the sequential Athena pipeline: ``python -m athena.run "<question>"``.

Also accepts ``--eval-id N`` to pull question N from the NASA evaluation set.
Prints one line per stage with its duration, the Markdown report, and the
blackboard history.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time

from google.adk.runners import InMemoryRunner
from google.genai import types

from athena.agents.a2a_stage import DEFAULT_CARD_URL
from athena.agents.supervisor import build_supervisor
from athena.blackboard import Blackboard, Task
from athena.tools import settings

APP_NAME = "athena"
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
        raise SystemExit('usage: python -m athena.run "<question>" | --eval-id N')
    return args.question


def server_is_up(card_url: str = DEFAULT_CARD_URL) -> bool:
    import httpx

    try:
        return httpx.get(card_url, timeout=3.0).status_code == 200
    except Exception:  # noqa: BLE001 - any failure means "not up"
        return False


def print_history(blackboard: Blackboard) -> None:
    print(f"{'seq':>3}  {'author':<11} {'section':<15} {'operation':<9} version")
    for entry in blackboard.history():
        print(
            f"{entry['seq']:>3}  {entry['author']:<11} {entry['section']:<15} "
            f"{entry['operation']:<9} {entry['version']}"
        )


async def run(question: str) -> int:
    blackboard = Blackboard.create(Task(question=question))
    runner = InMemoryRunner(agent=build_supervisor(blackboard.run_id), app_name=APP_NAME)
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID
    )

    print(f"\nrun_id: {blackboard.run_id}\nquestion: {question}\n")
    print("stages:")
    started = time.perf_counter()
    prompts = completions = 0

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=question)]),
    ):
        metadata = event.custom_metadata or {}
        if "stage" in metadata:
            print(f"  {metadata['stage']:<11} {metadata['stage_ms'] / 1000:6.1f}s")

        for line in _tool_lines(event):
            print(f"    {line}")

        text = _text_of(event)
        if "a2a_payload" in metadata:
            print(
                f"    [a2a request] {metadata['a2a_payload_bytes']}B "
                f"{metadata['a2a_payload']}"
            )
        if event.author in {"analyst", "writer", "critic"} and text.startswith("{"):
            print(f"    [a2a] {text}")
        if text.startswith("{") and '"fallback":true' in text:
            print(f"  ! {event.author}: writer fallback used (see ack)")

        usage = getattr(event, "usage_metadata", None)
        if usage is not None:
            prompts += getattr(usage, "prompt_token_count", 0) or 0
            completions += getattr(usage, "candidates_token_count", 0) or 0

    elapsed = time.perf_counter() - started

    report = blackboard.read("final_report")
    print("\n" + "=" * 70)
    if report:
        print(report[0].to_markdown())
    else:
        print("(no final report was written)")
    print("=" * 70)
    print(f"elapsed {elapsed:.1f}s | ADK-side tokens prompt {prompts} / completion {completions}")

    print("\nblackboard history:")
    print_history(blackboard)
    return 0


def _text_of(event) -> str:
    content = getattr(event, "content", None)
    if not content or not content.parts:
        return ""
    return "".join(part.text for part in content.parts if part.text and not part.thought)


def _short(value, limit: int = 110) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _tool_lines(event) -> list[str]:
    """Render the tool calls and results carried by one event."""
    content = getattr(event, "content", None)
    if not content or not content.parts:
        return []
    lines = []
    for part in content.parts:
        if part.function_call:
            lines.append(f"[tool] {part.function_call.name} {_short(part.function_call.args)}")
        if part.function_response:
            lines.append(f"[tool] {part.function_response.name} -> {_short(part.function_response.response)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    # The Markdown report can contain characters (curly quotes, ≤, •) that a
    # Windows console's default cp1252 cannot encode. Never crash on printing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="athena.run")
    parser.add_argument("question", nargs="?", help="the research question")
    parser.add_argument("--eval-id", type=int, help="question id from the NASA eval CSV")
    args = parser.parse_args(argv)

    if not server_is_up():
        print(
            "The CrewAI A2A server is not answering on 127.0.0.1:9001.\n"
            "Start it with:  python scripts/dev_up.py\n"
            "Then re-run this command.",
            file=sys.stderr,
        )
        return 1

    return asyncio.run(run(question_from_args(args)))


if __name__ == "__main__":
    raise SystemExit(main())
