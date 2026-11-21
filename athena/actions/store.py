"""Persistent review queue for actions that are only committed locally."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from athena.guardrails.secrets import redact_text

ActionKind = Literal["note", "email_draft", "calendar_event"]
ActionStatus = Literal["pending", "approved", "canceled"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: str, limit: int) -> str:
    return (redact_text(value) or "").strip()[:limit]


def _db_path() -> Path:
    return Path(os.getenv("ATHENA_ACTIONS_DB") or Path(os.getenv("ATHENA_DATA_DIR") or "data") / "actions.db")


class ActionStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else _db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                content TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
                start_at TEXT NOT NULL DEFAULT '', end_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('pending','approved','canceled')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_actions_status_created ON actions(status, created_at)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row else None

    def create(self, kind: ActionKind, title: str, content: str, source: str = "",
               start_at: str = "", end_at: str = "") -> dict:
        action_id, now = str(uuid.uuid4()), _now()
        title, content, source = _clean(title, 200), _clean(content, 20000), _clean(source, 500)
        if not title or not content:
            raise ValueError("title and content are required")
        with self._connect() as conn:
            conn.execute("""INSERT INTO actions
                (id,kind,title,content,source,start_at,end_at,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'pending',?,?)""",
                         (action_id, kind, title, content, source, start_at, end_at, now, now))
        return self.get(action_id) or {}

    def get(self, action_id: str) -> dict | None:
        with self._connect() as conn:
            return self._row(conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone())

    def list(self, status: ActionStatus | None = None) -> list[dict]:
        with self._connect() as conn:
            if status:
                rows = conn.execute("SELECT * FROM actions WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM actions ORDER BY created_at DESC").fetchall()
            return [dict(row) for row in rows]

    def transition(self, action_id: str, status: ActionStatus) -> dict | None:
        with self._connect() as conn:
            conn.execute("UPDATE actions SET status=?, updated_at=? WHERE id=? AND status='pending'",
                         (status, _now(), action_id))
            return self._row(conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone())
