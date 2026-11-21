"""Persistent reminders and search-backed watchlists for Athena."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from athena.api.guards import guard

router = APIRouter(prefix="/api/personal", tags=["personal agent"])
log = logging.getLogger(__name__)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, due_at TEXT NOT NULL,
 notes TEXT NOT NULL DEFAULT '', completed INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watchlists (
 id TEXT PRIMARY KEY, query TEXT NOT NULL, profile_id TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, next_check_at TEXT NOT NULL, last_checked_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS watchlist_results (
 watchlist_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL, snippet TEXT NOT NULL,
 fingerprint TEXT NOT NULL, PRIMARY KEY(watchlist_id,url));
CREATE TABLE IF NOT EXISTS watchlist_changes (
 id TEXT PRIMARY KEY, watchlist_id TEXT NOT NULL, kind TEXT NOT NULL,
 title TEXT NOT NULL, url TEXT NOT NULL, snippet TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(completed,due_at);
CREATE INDEX IF NOT EXISTS idx_watchlists_due ON watchlists(next_check_at);
CREATE INDEX IF NOT EXISTS idx_watch_changes ON watchlist_changes(watchlist_id,created_at);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("date-time must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    path = Path(os.getenv("ATHENA_DATA_DIR") or "data") / "personal.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class ReminderIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    due_at: datetime
    notes: str = Field(default="", max_length=2000)


class ReminderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    due_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)
    completed: bool | None = None


class WatchlistIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=2, max_length=300)
    profile_id: str = Field(default="", max_length=100)


def _reminder(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["completed"] = bool(item["completed"])
    item["overdue"] = not item["completed"] and item["due_at"] <= _stamp(_now())
    return item


@router.get("/reminders")
def list_reminders(request: Request, completed: bool | None = None) -> dict:
    guard(request)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders" + (" WHERE completed=?" if completed is not None else "") + " ORDER BY due_at",
            (int(completed),) if completed is not None else (),
        ).fetchall()
    return {"reminders": [_reminder(row) for row in rows]}


@router.post("/reminders", status_code=201)
def create_reminder(payload: ReminderIn, request: Request) -> dict:
    guard(request)
    try:
        due_at = _stamp(payload.due_at)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    item_id, created = str(uuid.uuid4()), _stamp(_now())
    with _db() as conn:
        conn.execute("INSERT INTO reminders VALUES(?,?,?,?,0,?)", (item_id, payload.title.strip(), due_at, payload.notes.strip(), created))
        row = conn.execute("SELECT * FROM reminders WHERE id=?", (item_id,)).fetchone()
    return {"reminder": _reminder(row)}


@router.patch("/reminders/{item_id}")
def update_reminder(item_id: str, payload: ReminderPatch, request: Request) -> dict:
    guard(request)
    values = payload.model_dump(exclude_unset=True)
    if "due_at" in values:
        if values["due_at"] is None:
            raise HTTPException(422, "due_at cannot be null")
        try:
            values["due_at"] = _stamp(values["due_at"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if "title" in values and values["title"] is None:
        raise HTTPException(422, "title cannot be null")
    if "notes" in values and values["notes"] is None:
        values["notes"] = ""
    if "completed" in values:
        if values["completed"] is None:
            raise HTTPException(422, "completed cannot be null")
        values["completed"] = int(values["completed"])
    if not values:
        raise HTTPException(422, "provide at least one field")
    with _db() as conn:
        cursor = conn.execute("UPDATE reminders SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), item_id))
        row = conn.execute("SELECT * FROM reminders WHERE id=?", (item_id,)).fetchone()
    if not cursor.rowcount:
        raise HTTPException(404, "reminder not found")
    return {"reminder": _reminder(row)}


@router.delete("/reminders/{item_id}")
def delete_reminder(item_id: str, request: Request) -> dict:
    guard(request)
    with _db() as conn:
        cursor = conn.execute("DELETE FROM reminders WHERE id=?", (item_id,))
    if not cursor.rowcount:
        raise HTTPException(404, "reminder not found")
    return {"deleted": True}


@router.get("/watchlists")
def list_watchlists(request: Request) -> dict:
    guard(request)
    with _db() as conn:
        rows = conn.execute("SELECT * FROM watchlists ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["change_count"] = conn.execute("SELECT COUNT(*) FROM watchlist_changes WHERE watchlist_id=?", (row["id"],)).fetchone()[0]
            item["recent_changes"] = [dict(change) for change in conn.execute("SELECT * FROM watchlist_changes WHERE watchlist_id=? ORDER BY created_at DESC, rowid DESC LIMIT 5", (row["id"],))]
            result.append(item)
    return {"watchlists": result}


@router.post("/watchlists", status_code=201)
def create_watchlist(payload: WatchlistIn, request: Request) -> dict:
    guard(request)
    item_id, now = str(uuid.uuid4()), _now()
    with _db() as conn:
        conn.execute("INSERT INTO watchlists(id,query,profile_id,created_at,next_check_at) VALUES(?,?,?,?,?)", (item_id, payload.query.strip(), payload.profile_id.strip(), _stamp(now), _stamp(now)))
        row = dict(conn.execute("SELECT * FROM watchlists WHERE id=?", (item_id,)).fetchone())
    row["change_count"], row["recent_changes"] = 0, []
    return {"watchlist": row}


def _perform_check(item_id: str) -> dict:
    with _db() as conn:
        item = conn.execute("SELECT * FROM watchlists WHERE id=?", (item_id,)).fetchone()
        if item is None:
            raise HTTPException(404, "watchlist not found")
        item = dict(item)
    from athena.chat.target import search_settings
    from athena.settings import defaults, profiles
    from athena.settings.vault import default_vault
    from athena.tools.web.search import web_search

    provider, endpoint = search_settings(item["profile_id"] or None)
    key = ""
    selected_id = item["profile_id"] or defaults.chat_profile_id() or profiles.active_id()
    profile = profiles.find(selected_id) if selected_id and selected_id != defaults.ENV_PROFILE else None
    if profile and provider == "firecrawl":
        key = default_vault().get(profiles.key_name(profile.id, "search")) or ""
    result = web_search(item["query"], 5, provider=provider, endpoint=endpoint, api_key=key)
    if not result.ok:
        raise HTTPException(502, result.error or "search failed")
    current: dict[str, dict] = {}
    changes: list[dict] = []
    for hit in result.results:
        fingerprint = hashlib.sha256((hit.title + "\0" + hit.snippet).encode()).hexdigest()
        current[hit.url] = {"title": hit.title[:300], "url": hit.url, "snippet": hit.snippet[:1200], "fingerprint": fingerprint}
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute("SELECT last_checked_at FROM watchlists WHERE id=?", (item_id,)).fetchone()
        if latest is None:
            raise HTTPException(404, "watchlist not found")
        now = _now()
        checked = _stamp(now)
        old = dict(conn.execute("SELECT url,fingerprint FROM watchlist_results WHERE watchlist_id=?", (item_id,)))
        for url, hit in current.items():
            if latest["last_checked_at"] and old.get(url) != hit["fingerprint"]:
                changes.append({
                    "id": str(uuid.uuid4()), "watchlist_id": item_id,
                    "kind": "updated" if url in old else "added", "title": hit["title"],
                    "url": url, "snippet": hit["snippet"], "created_at": checked,
                })
        conn.executemany(
            "INSERT OR REPLACE INTO watchlist_results VALUES(?,?,?,?,?)",
            [(item_id, r["url"], r["title"], r["snippet"], r["fingerprint"]) for r in current.values()],
        )
        conn.executemany("INSERT INTO watchlist_changes VALUES(?,?,?,?,?,?,?)", [(c["id"], item_id, c["kind"], c["title"], c["url"], c["snippet"], c["created_at"]) for c in changes])
        conn.execute("UPDATE watchlists SET last_checked_at=?,next_check_at=? WHERE id=?", (checked, _stamp(now + timedelta(hours=1)), item_id))
    return {"checked_at": checked, "results": list(current.values()), "changes": changes, "change_count": len(changes)}


@router.post("/watchlists/{item_id}/check")
def check_watchlist(item_id: str, request: Request) -> dict:
    guard(request)
    return {"check": _perform_check(item_id)}


@router.get("/watchlists/{item_id}/changes")
def list_changes(item_id: str, request: Request, limit: int = 50) -> dict:
    guard(request)
    with _db() as conn:
        if conn.execute("SELECT 1 FROM watchlists WHERE id=?", (item_id,)).fetchone() is None:
            raise HTTPException(404, "watchlist not found")
        rows = conn.execute("SELECT * FROM watchlist_changes WHERE watchlist_id=? ORDER BY created_at DESC LIMIT ?", (item_id, max(1, min(limit, 200)))).fetchall()
    return {"changes": [dict(row) for row in rows]}


@router.delete("/watchlists/{item_id}")
def delete_watchlist(item_id: str, request: Request) -> dict:
    guard(request)
    with _db() as conn:
        cursor = conn.execute("DELETE FROM watchlists WHERE id=?", (item_id,))
        conn.execute("DELETE FROM watchlist_results WHERE watchlist_id=?", (item_id,))
        conn.execute("DELETE FROM watchlist_changes WHERE watchlist_id=?", (item_id,))
    if not cursor.rowcount:
        raise HTTPException(404, "watchlist not found")
    return {"deleted": True}


def _check_due_watchlists() -> None:
    with _db() as conn:
        due = conn.execute(
            "SELECT id,next_check_at FROM watchlists WHERE next_check_at<=? ORDER BY next_check_at LIMIT 3",
            (_stamp(_now()),),
        ).fetchall()
    for row in due:
        try:
            _perform_check(row["id"])
        except Exception as exc:
            log.warning("Scheduled watchlist check failed (%s)", type(exc).__name__)
            with _db() as conn:
                conn.execute(
                    "UPDATE watchlists SET next_check_at=? WHERE id=? AND next_check_at=?",
                    (_stamp(_now() + timedelta(minutes=5)), row["id"], row["next_check_at"]),
                )


async def watchlist_worker(stop_event: asyncio.Event) -> None:
    """Check due entries hourly while the API process is running."""
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(_check_due_watchlists)
        except (OSError, sqlite3.Error) as exc:
            log.warning("Watchlist storage unavailable (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
