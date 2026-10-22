"""Blackboard storage interface and its error types.

The blackboard is a shared, persistent store rather than in-memory state:
agents run in separate processes and exchange only a ``run_id``. This interface
is the swap point between the local SQLite backend (``sqlite_store``) and a
hosted backend (Redis, Firestore) for cloud deployment.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from athena.blackboard.schemas import Task


class BlackboardError(Exception):
    """Base class for blackboard errors."""


class UnknownSectionError(BlackboardError):
    """Raised for a section name that is not in the registry."""


class SectionTypeError(BlackboardError):
    """Raised when a value does not match its section's model."""


class VersionConflictError(BlackboardError):
    """Raised when an expected_version does not match the stored version."""


class RunNotFoundError(BlackboardError):
    """Raised when writing to a run that does not exist."""


class BlackboardStore(ABC):
    """Storage contract for the blackboard.

    Implementations must be safe for concurrent writers in other threads and
    other processes, and must validate values on write and on read.
    """

    @abstractmethod
    def create_run(self, task: Task) -> str:
        """Persist a new run and return its run_id (generated when empty)."""

    @abstractmethod
    def write(
        self,
        run_id: str,
        section: str,
        value: BaseModel | list[BaseModel],
        author: str,
        expected_version: int | None = None,
    ) -> int:
        """Replace a section and return its new version.

        ``expected_version`` opts into optimistic concurrency: it must equal the
        current version or ``VersionConflictError`` is raised. A section that
        has never been written is at version 0.
        """

    @abstractmethod
    def append(self, run_id: str, section: str, item: BaseModel, author: str) -> int:
        """Append one item to a list section and return the new version."""

    @abstractmethod
    def read(
        self, run_id: str, section: str
    ) -> tuple[BaseModel | list[BaseModel], int] | None:
        """Return ``(validated_value, version)``, or None when the section is unset."""

    @abstractmethod
    def snapshot(self, run_id: str) -> dict[str, Any]:
        """Return every section of a run as validated models, plus their versions."""

    @abstractmethod
    def history(self, run_id: str) -> list[dict[str, Any]]:
        """Return the run's append-only provenance log in write order."""
