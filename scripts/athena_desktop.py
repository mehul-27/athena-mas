"""Run Athena's existing FastAPI app for the native desktop shell.

The desktop host keeps stdin open for the lifetime of this process. The API
binds a loopback socket and reports its port after Uvicorn starts accepting
connections.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 41773


class LoopbackOnly:
    """Reject requests that did not target this process's exact local origin."""

    def __init__(self, app: Any, port: int) -> None:
        self.app = app
        self.expected_host = f"127.0.0.1:{port}"
        self.expected_origin = f"http://{self.expected_host}"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        host = headers.get(b"host", b"").decode("latin-1").lower()
        origin = headers.get(b"origin")
        method = scope.get("method", "GET").upper()
        if host != self.expected_host or (
            method not in {"GET", "HEAD", "OPTIONS"}
            and origin is not None
            and origin.decode("latin-1").rstrip("/") != self.expected_origin
        ):
            body = b"Forbidden"
            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode("ascii"))],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def _configure_data_dir(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings").mkdir(exist_ok=True)
    (data_dir / "tmp").mkdir(exist_ok=True)
    # Match the container layout so all durable indexes and databases can be
    # reused by the existing backend without adding desktop-specific stores.
    paths = {
        "ATHENA_DATA_DIR": data_dir,
        "EVENTS_DB": data_dir / "events.db",
        "BLACKBOARD_DB": data_dir / "blackboard.db",
        "CHAT_DB": data_dir / "chat.db",
        "RATELIMIT_DB": data_dir / "ratelimit.db",
        "RATELIMIT_OUT_DB": data_dir / "ratelimit_out.db",
        "CHROMA_PATH": data_dir / "chroma",
        "CORPUS_DIR": data_dir / "corpus",
        "FASTEMBED_CACHE_PATH": data_dir / "fastembed_cache",
        "TMPDIR": data_dir / "tmp",
        "TMP": data_dir / "tmp",
        "TEMP": data_dir / "tmp",
        "MPLCONFIGDIR": data_dir / "mpl",
        "ATHENA_CREW_PID_FILE": data_dir / "desktop" / "crew.pid",
        "ATHENA_CREW_STOP_FILE": data_dir / "desktop" / "crew.stop",
    }
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", port))
        return sock
    except BaseException:
        sock.close()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="loopback port (0 asks the OS to choose one)")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    data_dir = args.data_dir.expanduser().resolve()
    _configure_data_dir(data_dir)
    os.chdir(REPO_ROOT)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    try:
        sock = _bind(args.port)
    except OSError as exc:
        print(f"Athena desktop could not bind loopback port {args.port}: {exc}",
              file=sys.stderr, flush=True)
        return 1
    port = sock.getsockname()[1]

    try:
        import uvicorn
        from athena.api.app import create_app

        app = LoopbackOnly(create_app(), port)
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_level="warning", access_log=False,
                                proxy_headers=False, timeout_graceful_shutdown=5)

        class DesktopServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                if self.started and not self.should_exit:
                    print(f"ATHENA_DESKTOP_PORT={port}", flush=True)

        server = DesktopServer(config)

        def watch_parent() -> None:
            try:
                while os.read(sys.stdin.fileno(), 1):
                    pass
            finally:
                server.should_exit = True

        threading.Thread(target=watch_parent, name="athena-desktop-parent", daemon=True).start()
        server.run(sockets=[sock])
        return 0
    except Exception as exc:
        print(f"Athena desktop backend failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
