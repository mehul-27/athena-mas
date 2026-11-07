"""Run lifecycle helpers: one terminal state, no orphaned workers or toolsets.

Two failures this module exists to prevent:

* a run that writes its summary and then keeps a live MCP stdio subprocess (or a
  worker coroutine) alive, so the process never exits;
* a run whose stages were cancelled or timed out but whose summary still looked
  like an ordinary (or even successful) run.

Every run therefore ends in exactly one of ``TERMINAL_STATES`` and every stage
that starts owned work gives it back in a ``finally`` block.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import types
from typing import Any, Iterable

TERMINAL_STATES = ("success", "failure", "cancelled", "timeout")
TERMINAL_SCOPE = "adk-wrapper"
CLOSE_TIMEOUT_S = 5.0

# Attribute names that can hold a toolset or a child agent.
_AGENT_ATTRS = (
    "tools",
    "toolsets",
    "sub_agents",
    "worker",
    "workers",
    "stages",
    "planner",
    "analyst",
    "writer",
    "critic",
    "researcher",
    "workers_by_role",
)
_MAX_DEPTH = 3

# Attribute values that are code, not owned work; reading them could also run
# a descriptor for an agent that never started.
_SKIPPED_VALUES = (
    types.FunctionType,
    types.MethodType,
    types.BuiltinFunctionType,
    types.BuiltinMethodType,
    classmethod,
    staticmethod,
    property,
    type,
)


class StageTimeout(TimeoutError):
    """A bounded stage or worker pool exceeded its deadline."""


def _env_seconds(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def pool_timeout_seconds() -> float:
    """Hard deadline for the concurrent research pool, in seconds."""
    return _env_seconds("ATHENA_RESEARCH_POOL_TIMEOUT_S", 900.0)


def stage_timeout_seconds() -> float:
    """Optional per-stage deadline; 0 (the default) disables it."""
    return _env_seconds("ATHENA_STAGE_TIMEOUT_S", 0.0)


def normalise_terminal(value: str | None) -> str:
    """Map free-form outcomes onto the four terminal states."""
    text = (value or "").strip().lower()
    if text in TERMINAL_STATES:
        return text
    if text in {"approved", "success", "succeeded", "ok"}:
        return "success"
    if text in {"cancelled", "canceled"}:
        return "cancelled"
    if text in {"timeout", "timed_out", "deadline"}:
        return "timeout"
    return "failure"


def terminal_from_outcome(outcome: str | None) -> str:
    """The terminal state implied by a run outcome."""
    text = (outcome or "").strip().lower()
    if text == "approved":
        return "success"
    return "failure"


def iter_toolsets(agent: Any, depth: int = _MAX_DEPTH) -> list[Any]:
    """Every toolset reachable from an agent, without importing ADK.

    Duck-typed on purpose: tests pass stub agents, and a real agent's tools may
    be plain function tools. Only objects with a callable ``close`` that are not
    agents themselves are returned.
    """
    found: list[Any] = []
    seen: set[int] = set()

    def walk(obj: Any, remaining: int) -> None:
        if obj is None or remaining < 0 or id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, (str, bytes, int, float, bool)):
            return
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value, remaining)
            return
        if isinstance(obj, (list, tuple, set, frozenset)):
            for value in obj:
                walk(value, remaining)
            return
        if callable(getattr(obj, "close", None)) and not hasattr(obj, "run_async"):
            if obj not in found:
                found.append(obj)
            return
        for attr in _AGENT_ATTRS:
            for value in _values(getattr(obj, attr, None)):
                walk(value, remaining - 1)
        # Nesting is not limited to the known names: an agent may own a child
        # under an application-specific attribute (``inner``, ``lead``, ...).
        for value in _extra_children(obj):
            walk(value, remaining - 1)

    walk(agent, depth)
    return found


def _extra_children(obj: Any) -> list[Any]:
    """Owned child values held under attribute names beyond ``_AGENT_ATTRS``.

    Only plain data and agent-like objects are returned. Functions, descriptors
    and classes are skipped, so discovery never calls borrowed code, and a plain
    value such as a name or a client is not mistaken for a toolset owner.
    """
    sources: list[dict[str, Any]] = []
    try:
        sources.append(vars(obj))
    except TypeError:
        pass
    for klass in getattr(type(obj), "__mro__", ()):  # class-level attributes
        try:
            sources.append(vars(klass))
        except TypeError:
            continue
    children: list[Any] = []
    for source in sources:
        for name, value in source.items():
            if name.startswith("_") or name in _AGENT_ATTRS:
                continue
            if isinstance(value, _SKIPPED_VALUES):
                continue
            if _holds_work(value):
                children.append(value)
    return children


def _holds_work(value: Any) -> bool:
    """True for containers of owned objects and for agent-like objects."""
    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return False
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return True
    if isinstance(value, _SKIPPED_VALUES) or callable(value):
        return False
    return any(hasattr(value, attr) for attr in _AGENT_ATTRS)


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    return [value]


async def close_toolsets(agents: Any, timeout: float = CLOSE_TIMEOUT_S) -> int:
    """Close every toolset reachable from ``agents``; never raises.

    Closing the toolset closes the MCP stdio session, which terminates the tool
    server subprocess. Without this a finished run can leave one subprocess per
    stage behind.
    """
    targets: list[Any] = []
    for agent in _values(agents):
        for toolset in iter_toolsets(agent):
            if toolset not in targets:
                targets.append(toolset)
    closed = 0
    for toolset in targets:
        try:
            result = toolset.close()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=timeout)
            closed += 1
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # A toolset that will not close must not abort the shutdown path.
            continue
        except Exception:  # noqa: BLE001 - cleanup is best effort
            continue
    return closed


async def cancel_tasks(tasks: Iterable[asyncio.Task]) -> int:
    """Cancel every task and wait for it, so none survives the call."""
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        with contextlib.suppress(Exception):
            await asyncio.gather(*pending, return_exceptions=True)
    return len(pending)


async def run_bounded(coroutines, timeout: float, label: str = "stage"):
    """Gather coroutines with a deadline, cancelling survivors on any exit.

    Returns their results in order. Raises ``StageTimeout`` when the deadline
    passes, and re-raises the first worker's exception after the others have
    been cancelled, so a failing worker never leaves a sibling running.
    """
    tasks = [asyncio.ensure_future(item) for item in coroutines]
    try:
        return await asyncio.wait_for(
            asyncio.gather(*tasks), timeout=timeout if timeout > 0 else None
        )
    except asyncio.TimeoutError as exc:
        await cancel_tasks(tasks)
        raise StageTimeout(f"{label} exceeded its {timeout:g}s deadline") from exc
    except BaseException:
        await cancel_tasks(tasks)
        raise
    finally:
        # Whatever happened, no worker coroutine may outlive this call.
        if any(not task.done() for task in tasks):
            await cancel_tasks(tasks)


def terminal_event_metadata(state: str, reason: str = "") -> dict[str, Any]:
    """Metadata for a stage/run end event carrying its single terminal state."""
    metadata: dict[str, Any] = {
        "terminal": normalise_terminal(state),
        "stage_scope": TERMINAL_SCOPE,
    }
    if reason:
        metadata["reason"] = reason[:400]
    return metadata
