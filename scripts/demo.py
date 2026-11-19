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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.1)
    return False


def start_crew_server(*, wait: bool = True) -> subprocess.Popen | None:
    """Start the server with the active profile's environment, if there is one."""
    script = REPO / "scripts" / "dev_up.py"
    env = dict(os.environ)
    try:
        from athena.api.activation import active_env

        env.update(active_env())
    except Exception:  # noqa: BLE001 - settings are optional
        pass
    process = subprocess.Popen([sys.executable, str(script)], cwd=str(REPO), env=env)
    if not wait:
        return process
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
    import uvicorn

    from athena.api.app import create_app

    url = f"http://{HOST}:{args.port}/"
    config = uvicorn.Config(create_app(), host=HOST, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    crew_start = None
    try:
        if not wait_for(f"http://{HOST}:{args.port}/healthz"):
            print(f"The API did not answer on {url}; see the traceback above.")
            return 1
        if not replay_only:
            print("Starting the CrewAI A2A server in the background...")
            try:
                crew_start = start_crew_server(wait=False)
            except OSError as exc:
                print(f"CrewAI could not start ({type(exc).__name__}); Chat and recordings are available.")
        print(f"Athena demo: {url} ({'Research starting' if crew_start else 'Chat and recordings'})")
        print("Press Ctrl+C to stop.")
        if not args.no_browser:
            webbrowser.open(url)

        crew_status_reported = False
        while thread.is_alive():
            if crew_start is not None and not crew_status_reported:
                result = crew_start.poll()
                if result is not None:
                    crew_status_reported = True
                    if result != 0:
                        print("CrewAI did not start; Chat and recordings are available.")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        if crew_start is not None and crew_start.poll() is None:
            crew_start.terminate()
            try:
                crew_start.wait(timeout=5)
            except subprocess.TimeoutExpired:
                crew_start.kill()
        server.should_exit = True
        thread.join(timeout=10)
        if crew_start is not None:
            stop_crew_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
