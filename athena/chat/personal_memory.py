"""Small, editable user memory shared by Athena chats.

Memory is local JSON, bounded, and intentionally learned only from explicit
first-person preference/profile statements. It never stores secret-shaped text.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athena.guardrails.secrets import contains_secret, redact_text

MAX_ITEMS = 40
MAX_CONTENT = 300
MAX_CONTEXT = 3000
_CATEGORIES = {"about_me", "preference", "ongoing_goal"}
_LOCK = threading.RLock()


def _path() -> Path:
    data = Path(os.getenv("ATHENA_DATA_DIR") or "data")
    return Path(os.getenv("ATHENA_PERSONAL_MEMORY_FILE") or data / "settings/personal_memory.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_item(category: str, content: str, *, allow_redact: bool = False) -> tuple[str, str]:
    category = str(category or "").strip().lower()
    content = str(content or "").strip()
    if category not in _CATEGORIES:
        raise ValueError("category must be about_me, preference, or ongoing_goal")
    if not content:
        raise ValueError("memory content cannot be empty")
    if len(content) > MAX_CONTENT:
        raise ValueError(f"memory content must be at most {MAX_CONTENT} characters")
    labeled_credential = re.search(
        r"(?i)\b(password|passphrase|api\s*key|access\s*token|secret|credential)\b\s*(?:is|=|:)\s*\S+",
        content,
    )
    if contains_secret(content) or labeled_credential:
        if not allow_redact:
            raise ValueError("memory cannot contain credentials, keys, or tokens")
        content = redact_text(content) or ""
    if not content.strip() or "[REDACTED]" in content or labeled_credential:
        raise ValueError("memory cannot contain credentials, keys, or tokens")
    return category, content


def list_items(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or _path()
    with _LOCK:
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    items = raw.get("items", []) if isinstance(raw, dict) else []
    if not isinstance(items, list):
        return []
    safe = []
    for item in items[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        try:
            category, content = _clean_item(item.get("category"), item.get("content"), allow_redact=True)
        except ValueError:
            continue
        safe.append({
            "id": str(item.get("id") or uuid.uuid4()),
            "category": category,
            "content": content,
            "created_at": str(item.get("created_at") or ""),
            "updated_at": str(item.get("updated_at") or ""),
            "source": "learned" if item.get("source") == "learned" else "manual",
        })
    return safe


def _write(items: list[dict[str, Any]], path: Path | None = None) -> None:
    target = path or _path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    try:
        prior = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prior = {}
    enabled = prior.get("enabled", True) if isinstance(prior, dict) else True
    temp.write_text(json.dumps({"version": 1, "enabled": enabled is not False, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)


def add_item(category: str, content: str, *, source: str = "manual", path: Path | None = None) -> dict[str, Any]:
    category, content = _clean_item(category, content)
    with _LOCK:
        items = list_items(path)
        key = (category, content.casefold())
        for existing in items:
            if (existing["category"], existing["content"].casefold()) == key:
                return existing
        if len(items) >= MAX_ITEMS:
            raise ValueError(f"memory can contain at most {MAX_ITEMS} items")
        now = _now()
        item = {"id": str(uuid.uuid4()), "category": category, "content": content,
                "created_at": now, "updated_at": now, "source": "learned" if source == "learned" else "manual"}
        _write([*items, item], path)
        return item


def update_item(item_id: str, *, category: str | None = None, content: str | None = None, path: Path | None = None) -> dict[str, Any] | None:
    with _LOCK:
        items = list_items(path)
        for item in items:
            if item["id"] == item_id:
                new_category, new_content = _clean_item(category or item["category"], content if content is not None else item["content"])
                item.update(category=new_category, content=new_content, updated_at=_now())
                _write(items, path)
                return item
        return None


def delete_item(item_id: str, path: Path | None = None) -> bool:
    with _LOCK:
        items = list_items(path)
        kept = [item for item in items if item["id"] != item_id]
        if len(kept) == len(items):
            return False
        _write(kept, path)
        return True


def learn_from_message(message: str, path: Path | None = None) -> dict[str, Any] | None:
    """Capture only explicit, durable first-person statements; no model call."""
    if not settings(path)["enabled"]:
        return None
    text = str(message or "").strip()
    if (not text or len(text) > 1000 or contains_secret(text)
            or re.search(r"\b(?:in|for) this (?:chat|project|conversation)\b", text, re.IGNORECASE)):
        return None
    name = re.fullmatch(
        r"(?:please\s+)?(?:my name is|call me)\s+((?!(?:at|later|tomorrow|when)\b)[^\W\d_][^\d_!?\r\n]{0,59})[.!]?",
        text, re.IGNORECASE,
    )
    if name:
        value = "My name is " + name.group(1).rstrip(". ")
        with _LOCK:
            previous = next((item for item in list_items(path) if item["source"] == "learned"
                             and item["category"] == "about_me" and item["content"].startswith("My name is ")), None)
            if previous:
                return previous if previous["content"] == value else update_item(previous["id"], content=value, path=path)
            return add_item("about_me", value, source="learned", path=path)
    patterns = (
        (r"^(?:please\s+)?remember\s+(?:that\s+)?i\s+(?:prefer|like|need|work|live|am|use)\b(.+)$", "preference"),
        (r"^i\s+(?:prefer|like)\s+(.+)$", "preference"),
        (r"^i\s+am\s+a\s+(.+)$", "about_me"),
        (r"^i'm\s+a\s+(.+)$", "about_me"),
        (r"^(?:my\s+ongoing\s+goal\s+is|i am working toward)\s+(.+)$", "ongoing_goal"),
    )
    for pattern, category in patterns:
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            fact = match.group(1).strip().rstrip(".!? ")
            if len(fact) < 4 or re.search(
                r"\b(password|api key|token|secret|credential|address|phone number|diagnosis|medical|religion|political affiliation|bank account)\b",
                fact, re.I,
            ):
                return None
            return add_item(category, fact[:MAX_CONTENT], source="learned", path=path)
    return None


def context_block(path: Path | None = None) -> str:
    items = list_items(path)
    if not items:
        return ""
    lines = ["Persistent personal memory. The user can override it in this chat. "
             "Use relevant notes as context, with the most recently updated first:"]
    for item in sorted(items, key=lambda item: item["updated_at"], reverse=True):
        lines.append(f"- {item['category']}: {item['content']}")
    return "\n".join(lines)[:MAX_CONTEXT]


def settings(path: Path | None = None) -> dict[str, bool]:
    try:
        raw = json.loads((path or _path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return {"enabled": raw.get("enabled") is not False} if isinstance(raw, dict) else {"enabled": True}


def set_enabled(enabled: bool, path: Path | None = None) -> bool:
    target = path or _path()
    with _LOCK:
        items = list_items(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps({"version": 1, "enabled": bool(enabled), "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)
    return bool(enabled)
