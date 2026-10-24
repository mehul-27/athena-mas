"""Process-wide hooks that turn library activity into events.

Installed once per process (runner and CrewAI server). Each hook looks up the
active recorder at call time, so a per-run recorder is honoured without passing
it through the libraries.
"""

from __future__ import annotations

from athena.blackboard import set_write_listener
from athena.config import ratelimit
from athena.observability.recorder import get_recorder

CREWAI_AGENTS = {"analyst", "writer", "critic"}


def framework_for(agent: str) -> str:
    return "crewai" if agent in CREWAI_AGENTS else "adk"


def _on_wait(waited_ms: float, tokens: int, agent: str) -> None:
    recorder = get_recorder()
    if recorder is None:
        return
    recorder.emit(
        "limiter_wait",
        agent or "supervisor",
        framework_for(agent),
        duration_ms=waited_ms,
        extra={"tokens_requested": tokens},
    )


def _on_write(section: str, version: int, author: str, operation: str) -> None:
    recorder = get_recorder()
    if recorder is None:
        return
    recorder.emit(
        "blackboard_write",
        author,
        framework_for(author),
        payload_summary=f"{operation} {section} v{version}",
        extra={"section": section, "version": version, "operation": operation},
    )


def install_hooks() -> None:
    """Route limiter waits and blackboard writes into the active recorder."""
    ratelimit.set_wait_listener(_on_wait)
    set_write_listener(_on_write)


def remove_hooks() -> None:
    ratelimit.set_wait_listener(None)
    set_write_listener(None)
