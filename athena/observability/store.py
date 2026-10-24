"""Append-only event log, shared by the ADK process and the CrewAI server.

Same safety style as the blackboard: WAL, busy timeout, one short transaction
per write, and SQL triggers that make UPDATE/DELETE impossible.
"""

from __future__ import annotations

import os
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from athena.observability.events import Event

BUSY_TIMEOUT_MS = 30_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    agent TEXT NOT NULL,
    framework TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (run_id, seq)
);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id, seq);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
"""


def default_db_path() -> Path:
    return Path(os.getenv("EVENTS_DB") or Path("data") / "events.db")


class EventStore(ABC):
    """Where monitoring events go."""

    @abstractmethod
    def emit(self, event: Event) -> int:
        """Append one event and return its per-run sequence number."""

    @abstractmethod
    def events(self, run_id: str) -> list[Event]:
        """Every event of a run, in order."""

    @abstractmethod
    def runs(self) -> list[str]:
        """Run ids, most recently active last."""


class SQLiteEventStore(EventStore):
    """SQLite backend, safe for threads and processes."""

    def __init__(self, path: str | Path | None = None, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
        self.path = str(path or default_db_path())
        self.busy_timeout_ms = busy_timeout_ms
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    @classmethod
    def from_env(cls) -> "SQLiteEventStore":
        return cls()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def emit(self, event: Event) -> int:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last FROM events WHERE run_id = ?",
                (event.run_id,),
            ).fetchone()
            seq = int(row["last"]) + 1
            stored = event.model_copy(update={"seq": seq})
            conn.execute(
                "INSERT INTO events (event_id, run_id, ts, seq, kind, agent, framework,"
                " payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (stored.event_id, stored.run_id, stored.ts, seq, stored.kind,
                 stored.agent, stored.framework, stored.to_json()),
            )
        return seq

    def events(self, run_id: str) -> list[Event]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT payload_json FROM events WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
        return [Event.from_json(row["payload_json"]) for row in rows]

    def runs(self) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT run_id, MIN(ts) AS first FROM events GROUP BY run_id"
                " ORDER BY first"
            ).fetchall()
        finally:
            conn.close()
        return [row["run_id"] for row in rows]

    def run_rows(self, run_id: str) -> list[dict[str, Any]]:
        """Raw rows, for the report's event table."""
        conn = self._connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT seq, ts, kind, agent, framework FROM events"
                    " WHERE run_id = ? ORDER BY seq",
                    (run_id,),
                ).fetchall()
            ]
        finally:
            conn.close()
