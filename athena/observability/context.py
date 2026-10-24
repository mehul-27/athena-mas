"""Which agent is currently running, so background hooks can attribute events.

Callbacks set this before an agent runs and clear it afterwards. Contextvars are
per task, so the parallel researchers do not overwrite each other.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT: ContextVar[str] = ContextVar("athena_agent", default="")


def current_agent() -> str:
    return _CURRENT.get() or "supervisor"


def set_agent(name: str):
    return _CURRENT.set(name or "")


def reset_agent(token) -> None:
    _CURRENT.reset(token)


@contextmanager
def agent_context(name: str):
    token = set_agent(name)
    try:
        yield
    finally:
        reset_agent(token)
