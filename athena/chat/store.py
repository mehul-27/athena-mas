"""SQLite persistence for interactive chat.

Deliberately separate from the research event store and Blackboard: a chat turn
is not a run, has no critic, and may run concurrently with other chats.

Message content is redacted on the way in *and* on the way out. A chat turn can
carry a pasted key, a bearer header or a token-shaped ID, and everything stored
here is later rendered by the conversation, message and transcript routes, so
the store — not each route — is the one place that has to get it right. The read
path is redacted too, so rows written before this rule existed cannot leak.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from athena.guardrails.secrets import redact_text, redact_tree

DEFAULT_TITLE = "New chat"
HISTORY_FALLBACK_TURNS = 20
HISTORY_FALLBACK_CHARS = 12000
MESSAGE_CHARS = 4000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT 'New chat',
    profile_id  TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'ok',
    provider        TEXT DEFAULT '',
    model           TEXT DEFAULT '',
    profile_id      TEXT DEFAULT '',
    capabilities    TEXT DEFAULT '[]',
    tools           TEXT DEFAULT '[]',
    sources         TEXT DEFAULT '[]',
    tool_events     TEXT DEFAULT '[]',
    usage           TEXT DEFAULT '',
    error           TEXT DEFAULT '',
    meta            TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages ON messages(conversation_id, created_at);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _safe_text(text: Any) -> str:
    """Redact a free-text field before it is written or returned."""
    if not text:
        return ""
    return redact_text(str(text)) or ""


def _loads(raw: str | None, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _db_path() -> Path:
    return Path(os.getenv("CHAT_DB") or Path("data") / "chat.db")


def derive_title(message: str) -> str:
    """A working title from the first message, with no extra model call."""
    words = _safe_text(message).strip().split()
    if not words:
        return DEFAULT_TITLE
    title = " ".join(words[:6])
    if len(title) > 60:
        title = title[:60].rstrip()
    if len(words) > 6:
        title += "…"
    return title


class ChatStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else _db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------------- conversations

    def create(self, title: str = DEFAULT_TITLE) -> dict[str, Any]:
        conv_id = str(uuid.uuid4())
        clean = _safe_text(title).strip()[:200] or DEFAULT_TITLE
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, profile_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (conv_id, clean, "", _now(), _now()),
            )
        return self.get(conv_id) or {}

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit or 50), 200)),),
            ).fetchall()
        return [self.summary(dict(row)) for row in rows]

    def rename(self, conversation_id: str, title: str) -> bool:
        clean = _safe_text(title).strip()[:200]
        if not clean:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (clean, _now(), conversation_id),
            )
            return cursor.rowcount > 0

    def set_profile(self, conversation_id: str, profile_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET profile_id = ?, updated_at = ? WHERE id = ?",
                ((profile_id or "")[:64], _now(), conversation_id),
            )

    def delete(self, conversation_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if not exists:
                return {"deleted": False, "removed_messages": 0}
            count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return {"deleted": True, "removed_messages": int(count)}

    # ---------------------------------------------------------------- messages

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str = "",
        *,
        status: str = "ok",
        provider: str = "",
        model: str = "",
        profile_id: str = "",
        capabilities: Iterable[str] | None = None,
        tools: Iterable[str] | None = None,
        sources: Iterable[Any] | None = None,
        tool_events: Iterable[Any] | None = None,
        usage: dict[str, Any] | None = None,
        error: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        created = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content, status, provider,"
                " model, profile_id, capabilities, tools, sources, tool_events, usage,"
                " error, meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    conversation_id,
                    role,
                    _safe_text(content),
                    status,
                    provider or "",
                    model or "",
                    profile_id or "",
                    _json(list(capabilities or [])),
                    _json(list(tools or [])),
                    _json(redact_tree(list(sources or []))),
                    _json(redact_tree(list(tool_events or []))),
                    _json(usage) if usage else "",
                    _safe_text(error),
                    _json(redact_tree(meta or {})),
                    created,
                ),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (created, conversation_id),
            )
        return self.get_message(message_id) or {}

    def update_message(self, message_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "content", "status", "provider", "model", "profile_id", "capabilities",
            "tools", "sources", "tool_events", "usage", "error", "meta",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {"capabilities", "tools", "sources", "tool_events"}:
                value = _json(redact_tree(list(value or [])))
            elif key == "usage":
                value = _json(value) if value else ""
            elif key == "meta":
                value = _json(redact_tree(value or {}))
            elif key in {"content", "error"}:
                value = _safe_text(value)
            assignments.append(f"{key} = ?")
            values.append(value)
        if not assignments:
            return self.get_message(message_id) or {}
        with self._connect() as conn:
            conn.execute(
                f"UPDATE messages SET {', '.join(assignments)} WHERE id = ?",
                (*values, message_id),
            )
        return self.get_message(message_id) or {}

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._message(row) if row else None

    def messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY rowid",
                (conversation_id,),
            ).fetchall()
        return [self._message(row) for row in rows]

    def history(
        self,
        conversation_id: str,
        limit: int = HISTORY_FALLBACK_TURNS,
        max_chars: int = HISTORY_FALLBACK_CHARS,
    ) -> list[dict[str, str]]:
        turns: list[dict[str, str]] = []
        for message in self.messages(conversation_id):
            if message["role"] not in {"user", "assistant"}:
                continue
            if message["status"] == "streaming":
                continue
            content = (message.get("content") or "").strip()
            if not content:
                continue
            turns.append({"role": message["role"], "content": content[:MESSAGE_CHARS]})
        turns = turns[-max(1, int(limit)) :]
        total = 0
        kept: list[dict[str, str]] = []
        for turn in reversed(turns):
            size = len(turn["content"])
            if kept and total + size > max_chars:
                break
            kept.append(turn)
            total += size
        kept.reverse()
        return kept

    def summary(self, conversation: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT usage FROM messages WHERE conversation_id = ? AND role = 'assistant'",
                (conversation["id"],),
            ).fetchall()
            count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation["id"],),
            ).fetchone()[0]
        tokens = 0
        for row in rows:
            usage = _loads(row["usage"], {})
            if isinstance(usage, dict):
                tokens += int(usage.get("tokens_in") or 0) + int(usage.get("tokens_out") or 0)
        return {
            "id": conversation["id"],
            "title": conversation["title"],
            "profile_id": conversation.get("profile_id") or "",
            "message_count": int(count),
            "total_tokens": tokens,
            "created_at": conversation.get("created_at") or "",
            "updated_at": conversation.get("updated_at") or "",
        }

    def summary_by_id(self, conversation_id: str) -> dict[str, Any] | None:
        conversation = self.get(conversation_id)
        return self.summary(conversation) if conversation else None

    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        # Read-side redaction: rows written before the write-side rule existed
        # (or by an older version) must not leak through the conversation and
        # transcript routes either.
        data["content"] = _safe_text(data.get("content"))
        data["error"] = _safe_text(data.get("error"))
        data["capabilities"] = _loads(data.get("capabilities"), [])
        data["tools"] = _loads(data.get("tools"), [])
        data["sources"] = redact_tree(_loads(data.get("sources"), []))
        data["tool_events"] = redact_tree(_loads(data.get("tool_events"), []))
        data["meta"] = redact_tree(_loads(data.get("meta"), {}))
        usage = _loads(data.get("usage"), {})
        data["usage"] = usage if isinstance(usage, dict) and usage else None
        return data
