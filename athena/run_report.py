"""Console rendering for the runner: tool lines, acks, the report and history.

Kept out of ``athena/run.py`` so the CLI stays a CLI.
"""

from __future__ import annotations

import json

from athena.blackboard import Blackboard


def parts_of(event):
    """The content parts of an ADK event (empty when there are none)."""
    content = getattr(event, "content", None)
    return content.parts if content and content.parts else []


def text_of(event) -> str:
    return "".join(part.text for part in parts_of(event) if part.text and not part.thought)


def _short(value, limit: int = 110) -> str:
    text = (
        json.dumps(value, ensure_ascii=False, default=str)
        if not isinstance(value, str)
        else value
    )
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def tool_lines(event) -> list[str]:
    """Tool calls and their results, one short line each."""
    lines = []
    for part in parts_of(event):
        if part.function_call:
            lines.append(f"[tool] {part.function_call.name} {_short(part.function_call.args)}")
        if part.function_response:
            lines.append(
                f"[tool] {part.function_response.name} -> "
                f"{_short(part.function_response.response)}"
            )
    return lines


def print_report(blackboard: Blackboard, elapsed: float) -> None:
    report = blackboard.read("final_report")
    print("\n" + "=" * 70)
    print(report[0].to_markdown() if report else "(no final report was written)")
    print("=" * 70)
    print(f"elapsed {elapsed:.1f}s")
    print("\nblackboard history:")
    print(f"{'seq':>3}  {'author':<15} {'section':<15} {'operation':<9} version")
    for entry in blackboard.history():
        print(
            f"{entry['seq']:>3}  {entry['author']:<15} {entry['section']:<15} "
            f"{entry['operation']:<9} {entry['version']}"
        )

def ack_warning(text: str) -> str | None:
    """One warning line for a failed or suspicious role acknowledgement."""
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None

    if not body.get("ok", True):
        return (
            f"WARNING {body.get('role')} failed: {body.get('error_type', 'Error')}: "
            f"{str(body.get('error', ''))[:200]}"
        )
    if body.get("section") and not (body.get("tokens_in") or body.get("tokens_out")):
        return f"WARNING {body.get('role')} reported zero tokens"
    return None