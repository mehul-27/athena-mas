"""Stage 2/3 driver: run the ADK supervisor and print every A2A delegation.

Usage:
    python spike/run_spike.py            # uses DEFAULT_PROMPT below
    python spike/run_spike.py "<prompt>" # override
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from spike.adk_agent import build_supervisor  # noqa: E402

DEFAULT_PROMPT = "Ask the analyst to comment on the number 42."
APP_NAME = "athena"
USER_ID = "student"


def _text_of(event: Any) -> str:
    """Concatenate the non-thought text parts of an ADK event."""
    content = getattr(event, "content", None)
    if not content or not content.parts:
        return ""
    return "".join(
        part.text for part in content.parts if part.text and not part.thought
    ).strip()


def _a2a_details(event: Any) -> tuple[Any, Any]:
    """Return the (request, response) pair ADK attached to an A2A event."""
    metadata = getattr(event, "custom_metadata", None) or {}
    return metadata.get("a2a:request"), metadata.get("a2a:response")


def _short(value: Any, limit: int = 240) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "..."


async def run(prompt: str) -> str:
    runner = InMemoryRunner(agent=build_supervisor(), app_name=APP_NAME)
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID
    )
    new_message = types.Content(role="user", parts=[types.Part(text=prompt)])

    final_answer = ""
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session.id,
        new_message=new_message,
    ):
        author = event.author
        request, response = _a2a_details(event)

        if request is not None or response is not None:
            print(f"[A2A] target={author}")
            print(f"      request : {_short(request)}")
            print(f"      response: {_short(response)}")

        text = _text_of(event)
        if text:
            print(f"[{author}] {text}")

        if event.is_final_response() and text:
            final_answer = text

    return final_answer


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT
    print(f"PROMPT: {prompt}\n")
    answer = asyncio.run(run(prompt))
    print(f"\nFINAL ANSWER: {answer}")
    return 0 if answer else 1


if __name__ == "__main__":
    raise SystemExit(main())
