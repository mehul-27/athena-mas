"""SQL schema, statements and row helpers for the SQLite blackboard backend.

Kept apart from ``sqlite_store`` so the store class reads as behaviour rather
than SQL text. History is append-only by construction: triggers reject UPDATE
and DELETE, so provenance cannot be rewritten by application code.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sections (
    run_id TEXT NOT NULL REFERENCES runs(run_id), section TEXT NOT NULL,
    version INTEGER NOT NULL, value_json TEXT NOT NULL,
    updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, section));
CREATE TABLE IF NOT EXISTS history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(run_id),
    section TEXT NOT NULL, operation TEXT NOT NULL, author TEXT NOT NULL,
    version INTEGER NOT NULL, timestamp TEXT NOT NULL, value_json TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS history_no_update BEFORE UPDATE ON history
BEGIN SELECT RAISE(ABORT, 'blackboard history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS history_no_delete BEFORE DELETE ON history
BEGIN SELECT RAISE(ABORT, 'blackboard history is append-only'); END;
"""

UPSERT = (
    "INSERT INTO sections (run_id, section, version, value_json, updated_by, updated_at)"
    " VALUES (?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(run_id, section) DO UPDATE SET version=excluded.version,"
    " value_json=excluded.value_json, updated_by=excluded.updated_by,"
    " updated_at=excluded.updated_at"
)
RECORD = (
    "INSERT INTO history (run_id, section, operation, author, version, timestamp,"
    " value_json) VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_run(conn: sqlite3.Connection, run_id: str) -> None:
    if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
        from athena.blackboard.store import RunNotFoundError

        raise RunNotFoundError(f"run {run_id!r} does not exist")


def current_version(conn: sqlite3.Connection, run_id: str, section: str) -> int:
    row = conn.execute(
        "SELECT version FROM sections WHERE run_id=? AND section=?", (run_id, section)
    ).fetchone()
    return row["version"] if row else 0


def save(
    conn: sqlite3.Connection,
    run_id: str,
    section: str,
    version: int,
    payload: str,
    author: str,
    operation: str,
    history_payload: str | None = None,
) -> None:
    """Write the section row and its history entry inside the caller's transaction."""
    timestamp = now()
    conn.execute(UPSERT, (run_id, section, version, payload, author, timestamp))
    conn.execute(
        RECORD,
        (
            run_id,
            section,
            operation,
            author,
            version,
            timestamp,
            history_payload or payload,
        ),
    )
