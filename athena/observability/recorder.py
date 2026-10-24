"""Recorder: the one thing the pipeline calls to log an event.

It never raises into the pipeline. If the store fails, the failure is counted
and one warning is printed per run, and the pipeline carries on.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager

from athena.observability.context import current_agent
from athena.observability.events import Event
from athena.observability.store import EventStore, SQLiteEventStore

_ACTIVE: "Recorder | None" = None


def monitoring_enabled() -> bool:
    return (os.getenv("ATHENA_MONITORING") or "1").strip() != "0"


def get_recorder() -> "Recorder | None":
    return _ACTIVE


def set_recorder(recorder: "Recorder | None") -> "Recorder | None":
    """Install the process-wide recorder and return the previous one."""
    global _ACTIVE
    previous, _ACTIVE = _ACTIVE, recorder
    return previous


class Recorder:
    """Writes this run's events. A no-op when monitoring is disabled."""

    def __init__(self, run_id: str, store: EventStore | None = None,
                 enabled: bool = True, clock=time.perf_counter) -> None:
        self.run_id = run_id
        self.store = store
        self.enabled = enabled
        self.failures = 0
        self._warned = False
        self._clock = clock

    @classmethod
    def for_run(cls, run_id: str, store: EventStore | None = None) -> "Recorder":
        enabled = monitoring_enabled()
        if store is None and enabled:
            store = SQLiteEventStore.from_env()
        return cls(run_id, store, enabled)

    def emit(self, kind: str, agent: str | None = None, framework: str = "adk",
             **fields) -> None:
        if not self.enabled:
            return
        try:
            event = Event(
                run_id=self.run_id,
                kind=kind,
                agent=agent or current_agent(),
                framework=framework,
                **fields,
            )
            self.store.emit(event)
        except Exception:  # noqa: BLE001 - monitoring must never break a run
            self.failures += 1
            if not self._warned:
                self._warned = True
                print(
                    f"[monitoring] event store unavailable; continuing without it "
                    f"({self.failures} failure(s))",
                    file=sys.stderr,
                )

    @contextmanager
    def span(self, kind: str, agent: str | None = None, framework: str = "adk",
             **fields):
        """Time a block and emit `kind`; emit an ``error`` event on failure."""
        started = self._clock()
        try:
            yield
        except Exception as exc:
            self.emit(
                "error",
                agent,
                framework,
                ok=False,
                duration_ms=round((self._clock() - started) * 1000, 2),
                error_type=type(exc).__name__,
                error_message=str(exc),
                **fields,
            )
            raise
        self.emit(
            kind,
            agent,
            framework,
            duration_ms=round((self._clock() - started) * 1000, 2),
            **fields,
        )

    # --- convenience wrappers used by the hooks -------------------------
    def model_call(self, agent: str, framework: str, tokens_in=None, tokens_out=None,
                   duration_ms=None, ok=True, **extra) -> None:
        self.emit("model_call", agent, framework, tokens_in=tokens_in,
                  tokens_out=tokens_out, duration_ms=duration_ms, ok=ok, extra=extra)

    def tool_call(self, agent: str, tool: str, args_summary: str = "", ok=True,
                  duration_ms=None, error: str | None = None) -> None:
        self.emit(
            "tool_call", agent, "adk", ok=ok, duration_ms=duration_ms,
            payload_summary=f"{tool}: {args_summary}"[:300],
            error_message=error,
            error_type=type(error).__name__ if error else None,
        )
