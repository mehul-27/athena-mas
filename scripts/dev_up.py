"""Start the CrewAI A2A server in the background and wait until it answers.

    python scripts/dev_up.py

PID goes to .run/crew_a2a.pid, logs to .run/crew_a2a.log. One terminal is
enough: start this, then run `python -m athena.run ...` in the same one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# `python scripts/dev_up.py` puts scripts/ on sys.path, not the repo root.
# Importing the active-profile helper requires the package root explicitly.
sys.path.insert(0, str(REPO_ROOT))
RUN_DIR = REPO_ROOT / ".run"
PID_FILE = RUN_DIR / "crew_a2a.pid"
LOG_FILE = RUN_DIR / "crew_a2a.log"
CARD_URL = "http://127.0.0.1:9001/.well-known/agent-card.json"


def card_answers(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(CARD_URL, timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - unreachable means "not up yet"
        return False


def main() -> int:
    if card_answers():
        print("ready (server was already running)")
        return 0

    RUN_DIR.mkdir(exist_ok=True)
    env = dict(os.environ)
    # Keep manual restarts consistent with the UI/demo path: the active profile
    # supplies the model, endpoint, key and search settings for both ADK and
    # CrewAI. Never print these values here.
    try:
        from athena.api.activation import active_env

        active = active_env()
        env.update(active)
        print(f"active profile env: {'loaded' if active else 'none'}")
    except Exception as exc:  # noqa: BLE001 - settings are optional in a bare checkout
        print(f"active profile env unavailable: {type(exc).__name__}")
    # Keep temp and caches inside the repo: this machine's system temp is not
    # reliably writable, and Chroma needs a usable temp directory.
    env.setdefault("TEMP", str(REPO_ROOT / ".tmp"))
    env.setdefault("TMP", env["TEMP"])
    env.setdefault("BLACKBOARD_DB", str(REPO_ROOT / "data" / "blackboard.db"))

    # What the server will actually be started with, in safe form: provider names,
    # redacted URLs and "set"/"missing" for key presence. The summary is built
    # from the merged env, so it proves the active profile really was applied
    # without ever putting a secret in a terminal, a log or a PID file.
    try:
        from athena.tools.diagnostics import env_summary

        summary = env_summary(env)
        print(
            "crew env: "
            f"model={summary['llm_model']} key={summary['llm_key']} "
            f"search={summary['search_provider']} "
            f"searxng={summary['searxng_url']} "
            f"fallback={summary['search_fallback']}"
        )
        if summary["llm_key"] == "missing":
            print(
                "warning: no LLM key in this environment; the server will fail its "
                "first model call. Set LLM_API_KEY in .env or activate a profile."
            )
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not block startup
        print(f"provider diagnostics unavailable: {type(exc).__name__}")

    print(f"crew env model: {env.get('LLM_MODEL') or '(default)'}")
    log = LOG_FILE.open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "athena.agents.crew_server"],
        cwd=str(REPO_ROOT),
        stdout=log,
        stderr=log,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    PID_FILE.write_text(str(process.pid), encoding="utf-8")

    deadline = time.time() + 90
    while time.time() < deadline:
        if card_answers():
            print("ready")
            print(f"pid {process.pid} | log {LOG_FILE}")
            return 0
        if process.poll() is not None:
            print(f"server exited early (code {process.returncode}); see {LOG_FILE}")
            return 1
        time.sleep(0.5)

    print(f"timed out waiting for {CARD_URL}; see {LOG_FILE}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
