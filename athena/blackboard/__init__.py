"""Athena's blackboard: shared, persistent, provenance-tracked agent state.

Agents run in separate processes, so they exchange a ``run_id`` over A2A and
read/write this store themselves. Typical use from an agent::

    bb = Blackboard.open(run_id)
    plan, version = bb.read("plan")
    bb.write("draft", Draft(text="..."), author="writer",
             expected_version=version)
    bb.append("evidence", item, author="researcher")
"""

from __future__ import annotations

from typing import Any

from athena.blackboard.schemas import (
    Analysis,
    Citation,
    Critique,
    Draft,
    EvidenceItem,
    FinalReport,
    GuardrailFlag,
    Plan,
    PlanStep,
    Task,
)
from athena.blackboard.sqlite_store import SQLiteStore
from athena.blackboard.store import (
    BlackboardError,
    BlackboardStore,
    RunNotFoundError,
    SectionTypeError,
    UnknownSectionError,
    VersionConflictError,
)

__all__ = [
    "Analysis",
    "Blackboard",
    "BlackboardError",
    "BlackboardStore",
    "Citation",
    "Critique",
    "Draft",
    "EvidenceItem",
    "FinalReport",
    "GuardrailFlag",
    "Plan",
    "PlanStep",
    "RunNotFoundError",
    "SQLiteStore",
    "SectionTypeError",
    "Task",
    "UnknownSectionError",
    "VersionConflictError",
]


_WRITE_LISTENER = None


def set_write_listener(listener) -> None:
    """Install a callback for every mutation: fn(section, version, author, operation).

    Off by default. The runner turns it on so monitoring can see section writes
    without the blackboard knowing anything about monitoring.
    """
    global _WRITE_LISTENER
    _WRITE_LISTENER = listener


def _notify_write(section: str, version: int, author: str, operation: str) -> None:
    listener = _WRITE_LISTENER
    if listener is None:
        return
    try:
        listener(section, version, author, operation)
    except Exception:  # noqa: BLE001 - a listener must never break a write
        pass


class Blackboard:
    """A run-scoped facade over a :class:`BlackboardStore`."""

    def __init__(self, store: BlackboardStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id

    @classmethod
    def open(cls, run_id: str, store: BlackboardStore | None = None) -> "Blackboard":
        """Bind to an existing run. Uses ``BLACKBOARD_DB`` when no store is given."""
        return cls(store or SQLiteStore.from_env(), run_id)

    @classmethod
    def create(cls, task: Task, store: BlackboardStore | None = None) -> "Blackboard":
        """Create a new run and return a blackboard bound to it."""
        store = store or SQLiteStore.from_env()
        return cls(store, store.create_run(task))

    def read(self, section: str):
        """Return ``(value, version)`` for a section, or None if unset."""
        return self.store.read(self.run_id, section)

    def write(
        self,
        section: str,
        value: Any,
        author: str,
        expected_version: int | None = None,
    ) -> int:
        """Replace a section and return its new version."""
        version = self.store.write(
            self.run_id, section, value, author, expected_version=expected_version
        )
        _notify_write(section, version, author, "write")
        return version

    def append(self, section: str, item: Any, author: str) -> int:
        """Append one item to a list section and return its new version."""
        version = self.store.append(self.run_id, section, item, author)
        _notify_write(section, version, author, "append")
        return version

    def snapshot(self) -> dict[str, Any]:
        """Return every section of this run as validated models, plus versions."""
        return self.store.snapshot(self.run_id)

    def history(self) -> list[dict[str, Any]]:
        """Return this run's append-only provenance log in write order."""
        return self.store.history(self.run_id)
