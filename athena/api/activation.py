"""Activate a provider profile: restart the CrewAI server with its environment.

The profile's secrets only ever exist in the child process environment built
here. They are not written to a file, logged, or returned by the API.

The server is started by running ``scripts/dev_up.py`` as a subprocess with the
profile environment merged in: dev_up already copies ``os.environ`` into the
server process, so passing it here is exactly "start the server with this env"
without duplicating dev_up's logic.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from athena.settings import profiles as store
from athena.settings.vault import default_vault

REPO = Path(__file__).resolve().parents[2]
CARD_URL = "http://127.0.0.1:9001/.well-known/agent-card.json"
CARD_TIMEOUT_S = 60


class ActivationError(RuntimeError):
    """The profile cannot be activated."""


def profile_env(profile_id: str) -> dict[str, str]:
    """The child-process environment for this profile (never logged)."""
    return store.env_for_profile(profile_id, default_vault())


def server_up(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(CARD_URL, timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - unreachable means down
        return False


def stop_server() -> None:
    subprocess.run([sys.executable, "scripts/dev_down.py"], cwd=str(REPO),
                   capture_output=True, text=True, timeout=60, check=False)


def start_server(env_extra: dict[str, str], wait_s: float = CARD_TIMEOUT_S) -> bool:
    child_env = dict(os.environ)
    child_env.update(env_extra)
    subprocess.Popen(
        [sys.executable, "scripts/dev_up.py"], cwd=str(REPO), env=child_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if server_up(timeout=1.5):
            return True
        time.sleep(0.5)
    return False


def activate(profile_id: str) -> dict:
    """Restart the server on this profile and remember it as active."""
    profile = store.find(profile_id)
    if profile is None:
        raise ActivationError(f"no profile {profile_id!r}")

    env = profile_env(profile_id)
    if not env.get("LLM_API_KEY"):
        raise ActivationError("no API key stored for this profile")

    was_up = server_up()
    stop_server()
    # A card that still answers after the stop means the old server survived.
    # Starting over it would report success without applying this profile.
    if was_up and server_up():
        raise ActivationError(
            "the existing CrewAI server did not stop; profile was not activated"
        )

    up = start_server(env)
    store.set_active(profile_id if up else "")
    return {"ok": up, "crew_server": "up" if up else "down",
            "profile": profile.public()}


def active_env() -> dict[str, str]:
    """Environment overrides for a run started from the API (empty if none)."""
    profile_id = store.active_id()
    if not profile_id or store.find(profile_id) is None:
        return {}
    try:
        return profile_env(profile_id)
    except Exception:  # noqa: BLE001 - a broken vault must not stop the API
        return {}


def active_summary() -> dict | None:
    """Name, model and host only - safe for /api/health."""
    profile_id = store.active_id()
    if not profile_id:
        return None
    profile = store.find(profile_id)
    if profile is None:
        return None
    from urllib.parse import urlparse

    return {
        "id": profile.id,
        "name": profile.name,
        "model": profile.model,
        "api_host": urlparse(profile.api_base).hostname or "",
        "has_key": default_vault().has(store.key_name(profile.id, "llm")),
    }
