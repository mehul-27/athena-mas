"""Non-secret defaults that are not tied to a provider profile.

Only one value exists so far: which profile Chat should use when the user has
not picked one for a conversation. `""` means "follow the active profile";
`"__env__"` means "use the process environment" (LLM_MODEL / LLM_API_KEY).
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULTS_FILE = Path("data/settings/defaults.json")
ENV_PROFILE = "__env__"


def _path(path: Path | None = None) -> Path:
    return path or DEFAULTS_FILE


def load(path: Path | None = None) -> dict:
    target = _path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def chat_profile_id(path: Path | None = None) -> str:
    return str(load(path).get("chat_profile_id") or "")


def set_chat_profile(profile_id: str, path: Path | None = None) -> str:
    value = (profile_id or "").strip()
    data = load(path)
    data["chat_profile_id"] = value
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return value
