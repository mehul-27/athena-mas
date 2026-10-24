"""Token usage for CrewAI calls, captured at the LiteLLM layer.

Every CrewAI call — plain, structured output and any validation re-ask — ends in
a ``litellm.completion``/``acompletion``. ``CrewOutput.token_usage`` does not
reflect those calls, so the numbers are read from the response here.

A scope is a mutable object held in a context variable: whatever thread or task
runs the call still holds the same object, so its totals come back to the
caller. Two scopes never share numbers.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from typing import Any, Callable

_SCOPE: ContextVar["Scope | None"] = ContextVar("athena_usage_scope", default=None)


class Scope:
    """Usage accumulated for one role invocation."""

    def __init__(self, role: str, request_id: str = "") -> None:
        self.role = role
        self.request_id = request_id
        self.tokens_in = 0
        self.tokens_out = 0
        self.reasoning_tokens = 0
        self.calls = 0

    def add(self, tokens_in: int, tokens_out: int, reasoning: int | None) -> None:
        self.tokens_in += int(tokens_in or 0)
        self.tokens_out += int(tokens_out or 0)
        self.reasoning_tokens += int(reasoning or 0)
        self.calls += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "reasoning_tokens": self.reasoning_tokens,
            "calls": self.calls,
        }


@contextmanager
def usage_scope(role: str, request_id: str = ""):
    """Open a scope for one role; closes and returns nothing on exit."""
    scope = Scope(role, request_id)
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def current_scope() -> Scope | None:
    return _SCOPE.get()


def run_in_scope(scope: Scope, func: Callable, *args, **kwargs):
    """Run ``func`` so it sees ``scope`` even from another thread."""
    def bound():
        token = _SCOPE.set(scope)
        try:
            return func(*args, **kwargs)
        finally:
            _SCOPE.reset(token)

    return copy_context().run(bound)


def _as_mapping(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            return dumped if isinstance(dumped, dict) else {}
        except Exception:  # noqa: BLE001 - usage parsing must never break a call
            return {}
    return {}


def usage_from_response(response_obj: Any) -> tuple[int, int, int | None]:
    """(prompt, completion, reasoning) from a litellm response, any shape."""
    if response_obj is None:
        return 0, 0, None

    if isinstance(response_obj, dict):
        usage = response_obj.get("usage")
    else:
        usage = getattr(response_obj, "usage", None)
    if usage is None:
        return 0, 0, None

    data = _as_mapping(usage)
    if not data:
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        details = getattr(usage, "completion_tokens_details", None)
        if prompt is None and completion is None and details is None:
            return 0, 0, None
        data = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "completion_tokens_details": details,
        }

    tokens_in = int(data.get("prompt_tokens") or 0)
    tokens_out = int(data.get("completion_tokens") or 0)

    details = data.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning = details.get("reasoning_tokens")
    else:
        reasoning = getattr(details, "reasoning_tokens", None)
    if reasoning is None:
        reasoning = data.get("reasoning_tokens")

    return tokens_in, tokens_out, int(reasoning) if reasoning else None


def _record(response_obj: Any) -> None:
    scope = current_scope()
    if scope is None:
        return
    try:
        tokens_in, tokens_out, reasoning = usage_from_response(response_obj)
    except Exception:  # noqa: BLE001 - monitoring must never break a call
        return
    scope.add(tokens_in, tokens_out, reasoning)


def build_logger():
    """The CustomLogger instance (imported lazily: litellm is heavy)."""
    from litellm.integrations.custom_logger import CustomLogger

    class UsageLogger(CustomLogger):
        def log_success_event(self, kwargs, response_obj, start_time, end_time):
            _record(response_obj)

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
            _record(response_obj)

    return UsageLogger()


_LOGGER = None


def install_usage_logger() -> bool:
    """Register the logger with litellm. Idempotent: returns True if newly added."""
    global _LOGGER
    import litellm

    if _LOGGER is None:
        _LOGGER = build_logger()

    callbacks = list(getattr(litellm, "callbacks", None) or [])
    if any(callback is _LOGGER for callback in callbacks):
        return False
    litellm.callbacks = callbacks + [_LOGGER]
    return True
