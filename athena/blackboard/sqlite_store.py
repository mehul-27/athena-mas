"""SQLite backend for the blackboard.

One connection per operation, WAL journaling and ``BEGIN IMMEDIATE`` so threads
and separate processes can write to the same file without losing updates.

Implement another ``BlackboardStore`` and swap this class out for a hosted store.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from athena.blackboard import sqlite_schema as sql
from athena.blackboard.schemas import Task
from athena.blackboard.store import (
    BlackboardError,
    BlackboardStore,
    VersionConflictError,
)
from athena.blackboard.validation import (
    check_appendable,
    check_section,
    check_value,
    decode_value,
    dump_item,
    dump_value,
)


class SQLiteStore(BlackboardStore):
    """SQLite backend, safe for concurrent threads and processes."""

    def __init__(self, db_path: str | Path, busy_timeout_ms: int = 30_000) -> None:
        self.db_path = str(db_path)
        self.busy_timeout_ms = busy_timeout_ms
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(sql.SCHEMA)
        finally:
            conn.close()

    @classmethod
    def from_env(cls) -> "SQLiteStore":
        """Build a store from ``BLACKBOARD_DB`` (default ``data/blackboard.db``)."""
        return cls(os.getenv("BLACKBOARD_DB", "data/blackboard.db"))

    def _connect(self) -> sqlite3.Connection:
        # Fresh connection per operation: sqlite3 connections are not safe to
        # share between threads.
        conn = sqlite3.connect(
            self.db_path, timeout=self.busy_timeout_ms / 1000, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        for pragma in (
            "journal_mode=WAL",
            f"busy_timeout={self.busy_timeout_ms}",
            "foreign_keys=ON",
            "synchronous=NORMAL",
        ):
            conn.execute(f"PRAGMA {pragma}")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            # IMMEDIATE takes the write lock up front, so concurrent appends are
            # serialised instead of racing between read and write.
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _rows(self, query: str, params: tuple) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    def create_run(self, task: Task) -> str:
        run_id = task.run_id or str(uuid.uuid4())
        stored = task.model_copy(update={"run_id": run_id})
        with self._tx() as conn:
            try:
                conn.execute(
                    "INSERT INTO runs (run_id, created_at) VALUES (?, ?)",
                    (run_id, sql.now()),
                )
            except sqlite3.IntegrityError as exc:
                raise BlackboardError(f"run {run_id!r} already exists") from exc
            sql.save(conn, run_id, "task", 1, stored.model_dump_json(), "system", "create")
        return run_id

    def write(
        self,
        run_id: str,
        section: str,
        value: BaseModel | list[BaseModel],
        author: str,
        expected_version: int | None = None,
    ) -> int:
        check_section(section)
        check_value(section, value)
        payload = dump_value(value)
        with self._tx() as conn:
            sql.require_run(conn, run_id)
            current = sql.current_version(conn, run_id, section)
            if expected_version is not None and expected_version != current:
                raise VersionConflictError(
                    f"section {section!r} is at version {current}, not {expected_version}"
                )
            version = current + 1
            sql.save(conn, run_id, section, version, payload, author, "write")
        return version

    def append(self, run_id: str, section: str, item: BaseModel, author: str) -> int:
        check_section(section)
        check_appendable(section, item)
        with self._tx() as conn:
            sql.require_run(conn, run_id)
            row = conn.execute(
                "SELECT version, value_json FROM sections WHERE run_id=? AND section=?",
                (run_id, section),
            ).fetchone()
            current = row["version"] if row else 0
            items = json.loads(row["value_json"]) if row else []
            items.append(item.model_dump(mode="json"))
            version = current + 1
            sql.save(
                conn,
                run_id,
                section,
                version,
                json.dumps(items, ensure_ascii=False),
                author,
                "append",
                dump_item(item),
            )
        return version

    def read(
        self, run_id: str, section: str
    ) -> tuple[BaseModel | list[BaseModel], int] | None:
        check_section(section)
        rows = self._rows(
            "SELECT version, value_json FROM sections WHERE run_id=? AND section=?",
            (run_id, section),
        )
        if not rows:
            return None
        return decode_value(section, rows[0]["value_json"]), rows[0]["version"]

    def snapshot(self, run_id: str) -> dict[str, Any]:
        rows = self._rows(
            "SELECT section, version, value_json FROM sections"
            " WHERE run_id=? ORDER BY section",
            (run_id,),
        )
        return {
            "run_id": run_id,
            "sections": {
                row["section"]: decode_value(row["section"], row["value_json"])
                for row in rows
            },
            "versions": {row["section"]: row["version"] for row in rows},
        }

    def history(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT seq, section, operation, author, version, timestamp, value_json"
            " FROM history WHERE run_id=? ORDER BY seq",
            (run_id,),
        )
        events = []
        for row in rows:
            event = dict(row)
            event["value"] = json.loads(event.pop("value_json"))
            events.append(event)
        return events
