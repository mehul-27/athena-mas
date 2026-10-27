"""Non-secret local preferences: the user's standing instruction.

Stored next to the profiles but never in the vault, because it is not a secret.
It is appended to the planner's and the writer's instructions *after* the system
rules and labelled as preferences, so it can adjust tone and depth but cannot
redefine the tools or the output contract.
"""

from __future__ import annotations

import json
from pathlib import Path

PREFS_FILE = Path("data/settings/preferences.json")
MAX_ABOUT = 800
LABEL = "The user's standing preferences (follow unless they conflict with the question):"


def _path(path: Path | None = None) -> Path:
    return path or PREFS_FILE


def load(path: Path | None = None) -> dict:
    target = _path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def about_me(path: Path | None = None) -> str:
    return str(load(path).get("about_me") or "").strip()


def set_about_me(text: str, path: Path | None = None) -> str:
    clean = (text or "").strip()
    if len(clean) > MAX_ABOUT:
        raise ValueError(f"about me must be at most {MAX_ABOUT} characters")
    data = load(path)
    data["about_me"] = clean
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return clean


def preference_block(text: str | None = None) -> str:
    """The block to append, or "" when the user has not written one."""
    value = (text if text is not None else about_me()).strip()
    if not value:
        return ""
    return f"{LABEL}\n{value}"


def with_preferences(instruction: str, text: str | None = None) -> str:
    """System rules first, then the preferences, on their own lines."""
    block = preference_block(text)
    if not block:
        return instruction
    return f"{instruction.rstrip()}\n\n{block}\n"
