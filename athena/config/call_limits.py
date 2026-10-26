"""Process-level limits for the model call in flight.

A ContextVar does not survive CrewAI's own threading, so the *server* records
which role is running here (it serialises roles) and the guard reads it to clamp
``max_tokens`` and ``timeout`` on every call it wraps. That is also where a
runaway completion is flagged.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager

DEFAULT_TIMEOUT_S = 120
RUNAWAY_FACTOR = 3

_lock = threading.Lock()
_role: str = ""
_cap: int = 0
runaway: dict | None = None


def timeout_s() -> float:
    value = (os.getenv("ATHENA_LLM_TIMEOUT_S") or "").strip()
    try:
        return float(value) if value else float(DEFAULT_TIMEOUT_S)
    except ValueError:
        return float(DEFAULT_TIMEOUT_S)


@contextmanager
def role_limits(role: str, cap: int):
    """Mark the role whose cap applies while this block runs."""
    global _role, _cap
    with _lock:
        _role, _cap = role, int(cap or 0)
    try:
        yield
    finally:
        with _lock:
            _role, _cap = "", 0


def current() -> tuple[str, int]:
    with _lock:
        return _role, _cap


def clamp(kwargs: dict) -> dict:
    """Apply the current role's cap and timeout to one call's kwargs."""
    _role_now, cap = current()
    limit = timeout_s()

    tokens = kwargs.get("max_tokens")
    if tokens is None:
        tokens = kwargs.get("max_completion_tokens")
    if cap and (tokens is None or int(tokens) > cap):
        kwargs["max_tokens"] = cap
        kwargs.pop("max_completion_tokens", None)

    current_timeout = kwargs.get("timeout")
    if current_timeout is None or float(current_timeout) > limit:
        kwargs["timeout"] = limit
    return kwargs


def note_completion(completion_tokens: int | None) -> None:
    """Flag a completion far beyond the role cap (the 132k-token call)."""
    global runaway
    role, cap = current()
    if not cap or not completion_tokens:
        return
    if int(completion_tokens) > RUNAWAY_FACTOR * cap:
        with _lock:
            runaway = {"role": role, "tokens": int(completion_tokens), "cap": cap}


def take_runaway() -> dict | None:
    """Return the pending runaway detail once, then clear it."""
    global runaway
    with _lock:
        detail, runaway = runaway, None
    return detail
