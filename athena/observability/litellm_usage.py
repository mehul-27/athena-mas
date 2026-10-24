"""Token usage for CrewAI calls, captured at the LiteLLM layer.

A context variable is not enough here: CrewAI rebuilds the LLM for every role
call, and its ``_init_litellm`` validator resets ``litellm.callbacks`` to the
LLM's own (empty) list — see ``crewai/llm.py:707``. A per-thread ContextVar set
before the call also does not survive whatever thread a callback fires on.

So the hook always adds to a *process-global* counter, and a scope reports the
**delta** across its own execution. ``usage_scope`` serialises role execution
with a process lock, so two roles cannot interleave their numbers.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

SETTLE_ENV = "ATHENA_USAGE_SETTLE_S"
DEFAULT_SETTLE_S = 2.0
STABLE_MS = 200


class _Totals:
    """Process-wide usage totals, safe to bump from any thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tokens_in = 0
        self.tokens_out = 0
        self.reasoning_tokens = 0
        self.calls = 0

    def add(self, tokens_in: int, tokens_out: int, reasoning: int | None) -> None:
        with self.lock:
            self.tokens_in += int(tokens_in or 0)
            self.tokens_out += int(tokens_out or 0)
            self.reasoning_tokens += int(reasoning or 0)
            self.calls += 1

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "reasoning_tokens": self.reasoning_tokens,
                "calls": self.calls,
            }


TOTALS = _Totals()
_ROLE_LOCK = threading.Lock()
_SCOPE: ContextVar["Scope | None"] = ContextVar("athena_usage_scope", default=None)


class Scope:
    """Usage for one role call: the delta of the global totals."""

    def __init__(self, role: str, start: dict[str, int] | None = None) -> None:
        self.role = role
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.reasoning_tokens = 0
        self._start = start or TOTALS.snapshot()

    def finish(self) -> "Scope":
        end = TOTALS.snapshot()
        self.tokens_in = max(0, end["tokens_in"] - self._start["tokens_in"])
        self.tokens_out = max(0, end["tokens_out"] - self._start["tokens_out"])
        self.reasoning_tokens = max(
            0, end["reasoning_tokens"] - self._start["reasoning_tokens"]
        )
        self.calls = max(0, end["calls"] - self._start["calls"])
        return self

    def as_dict(self) -> dict[str, int]:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "reasoning_tokens": self.reasoning_tokens,
            "calls": self.calls,
        }


def _settle(start_calls: int) -> None:
    """Wait for a late (async) callback before reading the delta."""
    try:
        timeout = float(os.getenv(SETTLE_ENV, DEFAULT_SETTLE_S))
    except ValueError:
        timeout = DEFAULT_SETTLE_S
    if timeout <= 0:
        return

    deadline = time.monotonic() + timeout
    last = TOTALS.snapshot()["calls"]
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        calls = TOTALS.snapshot()["calls"]
        if calls != last:
            last = calls
            stable_since = time.monotonic()
        elif calls > start_calls and (time.monotonic() - stable_since) >= STABLE_MS / 1000:
            return
        time.sleep(0.02)


@contextmanager
def usage_scope(role: str):
    """One role at a time per process; yields a scope filled in on exit."""
    start = TOTALS.snapshot()
    scope = Scope(role, start)
    token = _SCOPE.set(scope)
    with _ROLE_LOCK:
        try:
            yield scope
        finally:
            _SCOPE.reset(token)
            _settle(start["calls"])
            scope.finish()


def current_scope() -> Scope | None:
    return _SCOPE.get()


def _as_mapping(value) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            return dumped if isinstance(dumped, dict) else {}
        except Exception:  # noqa: BLE001 - usage parsing must never break a call
            return {}
    return {}


def usage_from_response(response_obj) -> tuple[int, int, int | None]:
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
        data = {"prompt_tokens": prompt, "completion_tokens": completion,
                "completion_tokens_details": details}

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
