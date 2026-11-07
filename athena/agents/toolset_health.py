"""Loud tracking of MCP toolsets that failed to load.

ADK does not fail a run when a toolset cannot start: ``base_llm_flow``'s tool
resolution catches the exception, logs

    Agent %s will run without the tools from toolset %s, which failed to load

and returns an empty tool list. The agent then runs with no tools at all,
gathers no evidence, and the run finishes looking like a normal (if empty)
result. That is exactly how a whole evaluation batch scored zero evidence while
every stage "completed".

This module makes that state observable and unmissable:

* :class:`HealthCheckedToolset` wraps the real ``McpToolset``. It only records a
  normal health event once tools actually materialise, and records a **failure**
  (``event="toolset"``, error level, ``degraded=True``) when the load raises or
  returns nothing. The failure is written to the event log and to the run's
  blackboard, and it is surfaced through :func:`failures` so the runner can mark
  the summary degraded and refuse to call the run a clean success.
* :func:`record_failure` emits that error event;
* :func:`failures` reads the events back for a run.

Nothing here retries or fabricates. A lost toolset is a recorded failure, and a
toolset that answers with no tools is a lost toolset: an agent that silently
cannot call anything is precisely the defect this exists to prevent.
"""

from __future__ import annotations

import sys
from typing import Any, Iterable

from google.adk.tools.mcp_tool import McpToolset

EVENT_MARKER = "toolset"
# Blackboard section holding one run's toolset health records. Named without a
# leading underscore so it is readable, and validated as free-form JSON so it
# never fights the typed section registry.
HEALTH_SECTION = "toolset_health"


def _recorder():
    from athena.observability.recorder import get_recorder

    return get_recorder()


# --- recording -------------------------------------------------------------


def record_failure(
    server: str,
    toolset: str,
    error: BaseException | str,
    required: Iterable[str] = (),
    agent: str = "researcher",
    framework: str = "adk",
    run_id: str = "",
    store: Any = None,
) -> dict[str, Any]:
    """Emit one ``error`` event for a toolset that could not be loaded.

    Never raises: monitoring must not turn a lost toolset into a crashed run.
    Returns the record it wrote so a caller can also put it on the blackboard.
    """
    error_type = (
        type(error).__name__ if isinstance(error, BaseException)
        else "ToolsetLoadFailed"
    )
    message = str(error) or f"{toolset} produced no usable tools"
    tools = [str(name) for name in required]
    target = server or toolset
    record = {
        "server": server,
        "toolset": toolset,
        "cause": error_type,
        "error": message[:300],
        "required_tools": tools,
        "agent": agent,
        "degraded": True,
    }

    recorder = _recorder()
    if recorder is not None:
        recorder.emit(
            "error",
            agent,
            framework,
            ok=False,
            error_type="ToolsetLoadFailed",
            error_message=f"{target}: {message}"[:300],
            payload_summary=f"toolset {target} failed to load",
            extra={"event": EVENT_MARKER, "message": message[:300], **record},
        )
    else:  # pragma: no cover - only before a recorder is installed
        # No recorder yet (e.g. an agent built outside a run). Still make the
        # lost capability visible on stderr rather than swallowing it.
        print(
            f"[toolset] {target} failed to load for {agent}: {message}",
            file=sys.stderr,
        )

    _note_on_blackboard(record, run_id=run_id, store=store)
    return record


def record_loaded(
    server: str,
    toolset: str,
    tool_names: Iterable[str] = (),
    agent: str = "researcher",
    framework: str = "adk",
) -> None:
    """Emit one informational event when a toolset did load real tools."""
    recorder = _recorder()
    if recorder is None:
        return
    names = [str(name) for name in tool_names]
    recorder.emit(
        "stage_end",
        agent,
        framework,
        ok=True,
        payload_summary=f"toolset {server or toolset} loaded: {', '.join(names) or '(none)'}",
        extra={
            "event": EVENT_MARKER,
            "server": server,
            "toolset": toolset,
            "tools": names,
            "degraded": False,
        },
    )


def _note_on_blackboard(record: dict[str, Any], run_id: str = "", store: Any = None) -> None:
    """Append the failure to the run's blackboard, when there is a run.

    The blackboard's section registry is closed, so the record lives in the
    event log (the authoritative source) and is only mirrored here when the run
    and section are both available. A missing blackboard never hides the event.
    """
    if not run_id:
        recorder = _recorder()
        run_id = getattr(recorder, "run_id", "") or ""
    if not run_id:
        return
    try:
        from athena.blackboard import Blackboard

        blackboard = Blackboard.open(run_id, store)
        _write_health(blackboard, record)
    except Exception:  # noqa: BLE001 - mirroring must never break a run
        return


def _write_health(blackboard: Any, record: dict[str, Any]) -> None:
    """Best-effort mirror onto the blackboard, tolerating an unknown section."""
    existing = None
    try:
        found = blackboard.read(HEALTH_SECTION)
        existing = found[0] if found else None
    except Exception:  # noqa: BLE001 - section may not be registered
        existing = getattr(blackboard, "_toolset_health", None)
    rows = list(existing) if isinstance(existing, list) else []
    if any(
        isinstance(row, dict)
        and row.get("server") == record.get("server")
        and row.get("cause") == record.get("cause")
        for row in rows
    ):
        return
    rows.append(dict(record))
    try:
        blackboard.write(HEALTH_SECTION, rows, author=record.get("agent") or "researcher")
    except Exception:  # noqa: BLE001 - unregistered section: keep it in memory
        setattr(blackboard, "_toolset_health", rows)


# --- reading ---------------------------------------------------------------


def failures(run_id: str, store: Any = None) -> list[dict[str, Any]]:
    """The toolsets this run could not load, deduplicated by server and cause."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in _events(run_id):
        if getattr(event, "kind", "") != "error":
            continue
        extra = getattr(event, "extra", None) or {}
        if extra.get("event") != EVENT_MARKER:
            continue
        key = (
            str(extra.get("server") or extra.get("toolset") or ""),
            str(extra.get("cause") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "server": key[0],
                "toolset": str(extra.get("toolset") or ""),
                "cause": key[1],
                "error": str(extra.get("message") or getattr(event, "error_message", "") or ""),
                "required_tools": list(extra.get("required_tools") or []),
                "agent": str(getattr(event, "agent", "") or ""),
            }
        )
    return rows


def _events(run_id: str) -> list[Any]:
    try:
        from athena.observability.store import SQLiteEventStore

        return SQLiteEventStore.from_env().events(run_id)
    except Exception:  # noqa: BLE001 - a status view must never break a run
        return []


def describe(rows: list[dict[str, Any]]) -> list[str]:
    """One short line per lost toolset, for the summary's stage errors."""
    return [
        f"toolset {row['server'] or row['toolset']} failed to load: "
        f"{row['error'] or row['cause']}"
        for row in rows
    ]


def note_on_summary(summary: Any, run_id: str, store: Any = None) -> list[dict[str, Any]]:
    """Mark a ``RunSummary`` degraded for every toolset this run lost.

    A run that lost a toolset is never a clean success: the capability the
    researcher needed was absent, so the evidence it could gather is bounded by
    that. Returns the failures so the caller can print them.

    The flag is set together with its reason (``degraded_reason``): a capability
    that went missing is exactly the kind of degradation a benchmark has to be
    able to name.
    """
    rows = failures(run_id, store)
    if not rows:
        return []
    # Imported here: `athena.summary` imports this module's siblings, so a
    # module-level import would close a cycle.
    from athena.summary import mark_degraded

    summary.toolset_failures = [dict(row) for row in rows]
    lines = describe(rows)
    for line in lines:
        if line not in summary.stage_errors:
            summary.stage_errors.append(line)
    mark_degraded(
        summary,
        "lost toolset(s): " + ", ".join(row["server"] or row["toolset"] for row in rows),
    )
    return rows


# --- the toolset that reports on itself ------------------------------------


def _tool_names(tools: Iterable[Any]) -> list[str]:
    return [str(getattr(tool, "name", "") or "") for tool in tools]


class HealthCheckedToolset(McpToolset):
    """An ``McpToolset`` that cannot be lost silently.

    ``get_tools`` is ADK's documented override point for a toolset whose listing
    may fail. Subclassing (rather than wrapping) keeps ADK's
    ``isinstance(toolset, BaseToolset)`` handling and the real prefixing in
    ``get_tools_with_prefix`` intact, so a healthy toolset behaves exactly like
    a plain ``McpToolset``.

    A load that raises, or that yields no tools while tools were required, is
    recorded as a failure and re-raised: ADK still drops the broken toolset, but
    the drop is now an observable error event, the run is marked degraded, and
    ``_mcp_session_manager`` is still closed by our lifecycle code.
    """

    # Health metadata. Class-level defaults keep every instance well-formed even
    # if it is unpickled before the attributes are restored.
    health_server: str = ""
    health_agent: str = "researcher"
    health_framework: str = "adk"
    health_required: list[str] = []
    health_run_id: str = ""
    health_store: Any = None
    health_raise_on_empty: bool = True

    def use_health(
        self,
        *,
        server: str = "",
        agent: str = "researcher",
        framework: str = "adk",
        required: Iterable[str] = (),
        run_id: str = "",
        store: Any = None,
        raise_on_empty: bool = True,
    ) -> "HealthCheckedToolset":
        """Attach what to report if this toolset's load fails."""
        self.health_server = server
        self.health_agent = agent
        self.health_framework = framework
        self.health_required = [str(name) for name in required]
        self.health_run_id = run_id
        self.health_store = store
        self.health_raise_on_empty = raise_on_empty
        return self

    async def get_tools(self, readonly_context=None) -> list[Any]:
        label = "MCPToolset"
        try:
            tools = await super().get_tools(readonly_context)
        except Exception as exc:  # noqa: BLE001 - record, then let ADK drop it
            record_failure(
                self.health_server,
                label,
                exc,
                required=self.health_required,
                agent=self.health_agent,
                framework=self.health_framework,
                run_id=self.health_run_id,
                store=self.health_store,
            )
            raise
        names = [name for name in _tool_names(tools) if name]
        if not tools and self.health_required:
            # A server that answered with no tools is as unusable as one that
            # never started, and ADK would otherwise treat it as a clean load.
            error = RuntimeError(
                f"{self.health_server or label} exposed no tools "
                f"(required: {', '.join(self.health_required)})"
            )
            record_failure(
                self.health_server,
                label,
                error,
                required=self.health_required,
                agent=self.health_agent,
                framework=self.health_framework,
                run_id=self.health_run_id,
                store=self.health_store,
            )
            if self.health_raise_on_empty:
                raise error
            return []
        record_loaded(
            self.health_server,
            label,
            names,
            agent=self.health_agent,
            framework=self.health_framework,
        )
        return list(tools)


__all__ = [
    "EVENT_MARKER",
    "HEALTH_SECTION",
    "HealthCheckedToolset",
    "describe",
    "failures",
    "note_on_summary",
    "record_failure",
    "record_loaded",
]
