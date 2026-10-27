"""One command for the demo: server, API, browser.

    python scripts/demo.py                 # live, starts the CrewAI server too
    python scripts/demo.py --replay-only   # no key needed, plays recorded runs

Nothing here prints a secret: presence is checked, values are not.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

HOST = "127.0.0.1"


def provider_state() -> dict:
    """Presence only - the key value never leaves this function."""
    import os

    import athena.config.llm as llm_config  # loads .env

    return {
        "configured": bool(os.getenv("LLM_API_KEY")),
        "model": bool(os.getenv("LLM_MODEL")),
        "host": llm_config.api_base(),
    }


def provider_ready() -> bool:
    state = provider_state()
    return state["configured"] and state["model"]


def wait_for(url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.4)
    return False


def start_crew_server() -> subprocess.Popen | None:
    """Start the server with the active profile's environment, if there is one."""
    script = REPO / "scripts" / "dev_up.py"
    env = dict(os.environ)
    try:
        from athena.api.activation import active_env

        env.update(active_env())
    except Exception:  # noqa: BLE001 - settings are optional
        pass
    process = subprocess.Popen([sys.executable, str(script)], cwd=str(REPO), env=env)
    process.wait(timeout=120)
    if process.returncode != 0:
        print("CrewAI server did not start; continuing in replay mode.")
        return None
    return process


def stop_crew_server() -> None:
    script = REPO / "scripts" / "dev_down.py"
    subprocess.run([sys.executable, str(script)], cwd=str(REPO), check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--replay-only", action="store_true")
    args = parser.parse_args(argv)

    replay_only = args.replay_only
    if not replay_only and not provider_ready():
        print("No provider key configured - starting in replay mode.")
        replay_only = True
    if not replay_only:
        print("Starting the CrewAI A2A server...")
        if start_crew_server() is None:
            replay_only = True

    import uvicorn

    from athena.api.app import create_app

    url = f"http://{HOST}:{args.port}/"
    config = uvicorn.Config(create_app(), host=HOST, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if not wait_for(f"http://{HOST}:{args.port}/api/health"):
        print(f"The API did not answer on {url}; see the traceback above.")
        return 1

    print(f"Athena demo: {url} ({'replay only' if replay_only else 'live'})")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)

    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if not replay_only:
            stop_crew_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
