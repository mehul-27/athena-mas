"""Reasoning-token extraction that survives every shape providers return.

ADK maps LiteLLM's ``completion_tokens_details.reasoning_tokens`` onto
``usage_metadata.thoughts_token_count``, but not every gateway fills that
nested object — some put ``reasoning_tokens`` at the top level, and ADK ignores
those. The client below remembers the raw usage, so the wrapper can put the
number where the plugin will find it.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_LAST_REASONING: ContextVar[int | None] = ContextVar("athena_reasoning", default=None)


def _as_dict(usage: Any) -> dict | None:
    if isinstance(usage, dict):
        return usage
    if hasattr(usage, "model_dump"):
        try:
            dumped = usage.model_dump()
            return dumped if isinstance(dumped, dict) else None
        except Exception:  # noqa: BLE001 - extraction must never break a call
            return None
    return None


def reasoning_from_usage(usage: Any) -> int | None:
    """Read reasoning tokens from a dict/object, nested or top level."""
    if usage is None:
        return None

    data = _as_dict(usage)
    if data is None:
        details = getattr(usage, "completion_tokens_details", None)
        # ``details`` may itself be a dict (litellm) or a model (openai SDK).
        if isinstance(details, dict):
            nested = details.get("reasoning_tokens")
        else:
            nested = getattr(details, "reasoning_tokens", None) if details else None
        value = nested if nested is not None else getattr(usage, "reasoning_tokens", None)
        return int(value) if value else None

    details = data.get("completion_tokens_details")
    if isinstance(details, dict):
        value = details.get("reasoning_tokens")
    else:
        value = getattr(details, "reasoning_tokens", None)
    if value is None:
        value = data.get("reasoning_tokens")
    return int(value) if value else None


def set_last_reasoning(value: int | None) -> None:
    _LAST_REASONING.set(value)


def last_reasoning() -> int | None:
    return _LAST_REASONING.get()
