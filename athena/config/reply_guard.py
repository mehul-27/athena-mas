"""Detecting and reporting an empty reply from a reasoning model.

A reasoning model can spend its whole output budget on hidden reasoning and
return nothing visible with ``finish_reason == "length"``. This module owns the
detection, the doubled-budget retry value, and the ``empty_reply`` event, so the
two model wrappers stay about plumbing.
"""

from __future__ import annotations

from typing import Any

from athena.observability.context import current_agent
from athena.observability.recorder import get_recorder

MAX_RETRY_TOKENS = 4000


def doubled(current: int | None, fallback: int = 1000) -> int:
    """Twice the current budget, never above MAX_RETRY_TOKENS."""
    return min((current or fallback) * 2, MAX_RETRY_TOKENS)


def note_empty_reply(framework: str, budget: int | None) -> None:
    """Record one ``empty_reply`` error event. Never raises."""
    recorder = get_recorder()
    if recorder is None:
        return
    recorder.emit(
        "error",
        current_agent(),
        framework,
        ok=False,
        error_type="empty_reply",
        error_message="model returned no visible text (finish_reason=length); retrying",
        payload_summary=f"empty reply at max_tokens={budget}",
    )


def has_text(response: Any) -> bool:
    """True when a response carries at least one non-thought text part."""
    content = getattr(response, "content", None)
    parts = getattr(content, "parts", None) or []
    return any(
        getattr(part, "text", None) and not getattr(part, "thought", False)
        for part in parts
    )


def is_length(response: Any) -> bool:
    reason = getattr(response, "finish_reason", None)
    return reason is not None and "LENGTH" in str(reason).upper()


def empty_and_truncated(responses: list) -> bool:
    """No visible text anywhere, and at least one response hit the length cap."""
    return not any(has_text(r) for r in responses) and any(is_length(r) for r in responses)
