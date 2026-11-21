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
CREATE INDEX IF NOT EXISTS idx_chat_message_order ON messages(conversation_id);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_items (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('chat', 'report', 'document', 'reminder', 'watchlist', 'action')),
    ref TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, kind, ref)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_chat ON workspace_items(ref) WHERE kind = 'chat';
"""

_SUMMARY_FIELDS = """
    (SELECT workspace_id FROM workspace_items WHERE kind = 'chat' AND ref = c.id) AS workspace_id,
    (SELECT COUNT(*) FROM messages WHERE conversation_id = c.id) AS message_count,
    (SELECT COALESCE(SUM(
        COALESCE(CAST(json_extract(usage, '$.tokens_in') AS INTEGER), 0) +
        COALESCE(CAST(json_extract(usage, '$.tokens_out') AS INTEGER), 0)
    ), 0) FROM messages
       WHERE conversation_id = c.id AND role = 'assistant' AND json_valid(usage)
    ) AS total_tokens
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


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "profile_id": row.get("profile_id") or "",
        "workspace_id": row.get("workspace_id") or "",
        "message_count": row["message_count"],
        "total_tokens": row["total_tokens"],
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


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
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------------- conversations

    def create(self, title: str = DEFAULT_TITLE, *, workspace_id: str | None = None) -> dict[str, Any]:
        conv_id = str(uuid.uuid4())
        clean = _safe_text(title).strip()[:200] or DEFAULT_TITLE
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, profile_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (conv_id, clean, "", _now(), _now()),
            )
            if workspace_id:
                self._link_chat(conn, conv_id, workspace_id)
        return self.get(conv_id) or {}

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 50, *, workspace_id: str | None = None) -> list[dict[str, Any]]:
        where = "" if workspace_id is None else """
            WHERE EXISTS (SELECT 1 FROM workspace_items
                          WHERE kind='chat' AND ref=c.id AND workspace_id=?)
        """
        params = [] if workspace_id is None else [workspace_id]
        params.append(max(1, min(int(limit or 50), 200)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT c.*, {_SUMMARY_FIELDS} FROM conversations c {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_summary(dict(row)) for row in rows]

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
            conn.execute("DELETE FROM workspace_items WHERE kind='chat' AND ref=?", (conversation_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return {"deleted": True, "removed_messages": int(count)}

    # ---------------------------------------------------------------- workspaces

    def workspaces(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT w.*, (SELECT COUNT(*) FROM workspace_items
                             WHERE workspace_id=w.id) AS item_count
                FROM workspaces w ORDER BY name COLLATE NOCASE, id
            """)
            return [dict(row) for row in rows]

    def workspace_items(self, workspace_id: str) -> list[dict[str, Any]] | None:
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
                return None
            rows = conn.execute("""
                SELECT w.kind, w.ref, COALESCE(c.title, w.label) AS label, w.created_at
                FROM workspace_items w LEFT JOIN conversations c ON w.kind='chat' AND c.id=w.ref
                WHERE w.workspace_id=? ORDER BY w.created_at DESC, w.kind, w.ref
            """, (workspace_id,))
            return [dict(row) for row in rows]

    def save_workspace(self, name: str, notes: str, workspace_id: str | None = None) -> dict[str, Any] | None:
        name, notes = _safe_text(name).strip(), _safe_text(notes).strip()
        with self._connect() as conn:
            if workspace_id is None:
                workspace_id = str(uuid.uuid4())
                conn.execute("INSERT INTO workspaces VALUES (?, ?, ?, ?)", (workspace_id, name, notes, _now()))
            else:
                conn.execute("UPDATE workspaces SET name=?, notes=?, updated_at=? WHERE id=?",
                             (name, notes, _now(), workspace_id))
            row = conn.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
            return dict(row) if row else None

    def delete_workspace(self, workspace_id: str) -> bool:
        with self._connect() as conn:
            return bool(conn.execute("DELETE FROM workspaces WHERE id=?", (workspace_id,)).rowcount)

    @staticmethod
    def _link_chat(conn: sqlite3.Connection, chat_id: str, workspace_id: str) -> None:
        if not conn.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
            raise ValueError("Project not found")
        conn.execute("DELETE FROM workspace_items WHERE kind='chat' AND ref=?", (chat_id,))
        conn.execute("INSERT INTO workspace_items VALUES (?, 'chat', ?, '', ?)",
                     (workspace_id, chat_id, _now()))

    def link_workspace_item(self, workspace_id: str, kind: str, ref: str, label: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if kind == "chat":
                if not conn.execute("SELECT 1 FROM conversations WHERE id=?", (ref,)).fetchone():
                    raise ValueError("Chat not found")
                self._link_chat(conn, ref, workspace_id)
                return
            cursor = conn.execute("""
                INSERT INTO workspace_items (workspace_id, kind, ref, label, created_at)
                SELECT id, ?, ?, ?, ? FROM workspaces WHERE id=?
                ON CONFLICT(workspace_id, kind, ref) DO UPDATE SET label=excluded.label
            """, (kind, ref, _safe_text(label)[:240], _now(), workspace_id))
            if not cursor.rowcount:
                raise ValueError("Project not found")

    def unlink_workspace_item(self, workspace_id: str, kind: str, ref: str) -> bool:
        with self._connect() as conn:
            return bool(conn.execute("DELETE FROM workspace_items WHERE workspace_id=? AND kind=? AND ref=?",
                                     (workspace_id, kind, ref)).rowcount)

    def workspace_context(self, conversation_id: str) -> str:
        with self._connect() as conn:
            project = conn.execute("""
                SELECT w.* FROM workspaces w JOIN workspace_items i ON i.workspace_id=w.id
                WHERE i.kind='chat' AND i.ref=?
            """, (conversation_id,)).fetchone()
            if project is None:
                return ""
            related = conn.execute("""
                SELECT c.title, m.role, substr(m.content,1,320) AS excerpt
                FROM workspace_items i JOIN conversations c ON c.id=i.ref
                JOIN messages m ON m.conversation_id=c.id
                WHERE i.workspace_id=? AND i.kind='chat' AND i.ref<>? AND m.status='ok'
                      AND m.role IN ('user', 'assistant') AND m.content <> ''
                ORDER BY m.rowid DESC LIMIT 6
            """, (project["id"], conversation_id)).fetchall()
            links = conn.execute("""
                SELECT kind,label FROM workspace_items WHERE workspace_id=? AND kind<>'chat'
                ORDER BY created_at DESC LIMIT 8
            """, (project["id"],)).fetchall()
        lines = [f"Project: {project['name']}", project["notes"][:2400]]
        if related:
            lines.append("Recent excerpts from other chats in this project:")
            lines.extend(f"- {item['title']} ({item['role']}): {item['excerpt']}" for item in reversed(related))
        if links:
            lines.append("Items filed in this project:")
            lines.extend(f"- {item['kind']}: {item['label']}" for item in links)
        return _safe_text("\n".join(lines))

    def recall_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT id, role, substr(content,1,4000) AS content
                FROM messages
                WHERE conversation_id=? AND status='ok' AND role IN ('user','assistant')
                ORDER BY rowid DESC LIMIT 400
            """, (conversation_id,)).fetchall()
        return [{**dict(row), "content": _safe_text(row["content"])} for row in reversed(rows)]

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
        limit = max(1, int(limit))
        total = 0
        kept: list[dict[str, str]] = []
        with self._connect() as conn:
            recent = conn.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ?"
                " AND role IN ('user', 'assistant') AND status != 'streaming' ORDER BY rowid DESC",
                (conversation_id,),
            )
            for row in recent:
                content = _safe_text(row["content"]).strip()[:MESSAGE_CHARS]
                if not content:
                    continue
                if kept and total + len(content) > max_chars:
                    break
                kept.append({"role": row["role"], "content": content})
                total += len(content)
                if len(kept) >= limit:
                    break
        kept.reverse()
        return kept

    def summary(self, conversation: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            stats = conn.execute(
                f"SELECT {_SUMMARY_FIELDS} FROM (SELECT ? AS id) c",
                (conversation["id"],),
            ).fetchone()
        return _summary({**conversation, **dict(stats)})

    def summary_by_id(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT c.*, {_SUMMARY_FIELDS} FROM conversations c WHERE c.id = ?",
                (conversation_id,),
            ).fetchone()
        return _summary(dict(row)) if row else None

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
