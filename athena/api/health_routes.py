"""Health, lifecycle and logging for the deployed app.

Three probes, three different jobs:

* ``GET /healthz`` - liveness only. No I/O, no locks, no clock: the platform
  restarts a process whose loop cannot answer, and nothing else.
* ``GET /readyz`` - readiness. The data volume must be writable, the events
  database must open, and the loopback A2A crew server (port 9001) should
  answer its card. The card probe is cached for a few seconds and runs in the
  threadpool (this is a ``def`` handler), so a hung crew server can never stall
  the event loop or the other requests.
* ``GET /api/health`` - the demo page's own status panel. Unchanged, for UI
  compatibility; it keeps its richer body and its blocking probe.

This module also owns the two hooks the API needs to survive a container
lifecycle: :func:`build_lifespan` (create the writable directories at startup;
stop the active run and the child crew server at shutdown) and
:func:`configure_logging` (one JSON log line per record, so a platform log
collector can read it).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

log = logging.getLogger("athena.api")

router = APIRouter()

CREW_CARD_URL = os.getenv(
    "ATHENA_CREW_CARD_URL", "http://127.0.0.1:9001/.well-known/agent-card.json"
)
CARD_TIMEOUT_S = float(os.getenv("ATHENA_CARD_TIMEOUT_S", "0.5"))
CARD_TTL_S = float(os.getenv("ATHENA_CARD_CACHE_S", "5"))
SHUTDOWN_TIMEOUT_S = float(os.getenv("ATHENA_SHUTDOWN_TIMEOUT_S", "5"))

_CARD_CACHE: dict = {"at": 0.0, "up": False, "detail": "not probed yet"}


# --- identity ---------------------------------------------------------------


def version_info() -> dict:
    """Version and commit, from the image (baked as env) - never from git."""
    return {
        "version": (
            os.getenv("ATHENA_VERSION")
            or os.getenv("APP_VERSION")
            or "0.1.0"
        ),
        "commit": (
            os.getenv("ATHENA_GIT_SHA")
            or os.getenv("GIT_SHA")
            or os.getenv("SOURCE_COMMIT")
            or "unknown"
        ),
    }


def data_dir() -> Path:
    """The directory that must be the mounted volume in production."""
    return Path(os.getenv("ATHENA_DATA_DIR") or "data")


def events_db() -> Path:
    return Path(os.getenv("EVENTS_DB") or data_dir() / "events.db")


# --- the checks -------------------------------------------------------------


def volume_writable(directory: Path | None = None) -> tuple[bool, str]:
    """Prove the data directory accepts a write (the volume is mounted)."""
    target = Path(directory or data_dir())
    try:
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target, prefix=".readyz-", delete=True) as handle:
            handle.write(b"ok")
            handle.flush()
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def database_openable(path: Path | None = None) -> tuple[bool, str]:
    """Prove SQLite can open (and create, on first boot) the events database."""
    target = Path(path or events_db())
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(target, timeout=1.0) as conn:
            conn.execute("select 1").fetchone()
            conn.execute("pragma user_version").fetchone()
        return True, ""
    except (sqlite3.Error, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _probe_card(timeout: float) -> tuple[bool, str]:
    import httpx

    try:
        response = httpx.get(CREW_CARD_URL, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - unreachable means "down"
        return False, type(exc).__name__
    if response.status_code == 200:
        return True, ""
    return False, f"HTTP {response.status_code}"


def crew_state(timeout: float = CARD_TIMEOUT_S) -> dict:
    """The A2A card, cached briefly so /readyz stays cheap under a probe."""
    now = time.monotonic()
    if now - float(_CARD_CACHE["at"]) < CARD_TTL_S and _CARD_CACHE["at"]:
        return {
            "ok": bool(_CARD_CACHE["up"]),
            "detail": _CARD_CACHE["detail"],
            "cached": True,
            "age_s": round(now - float(_CARD_CACHE["at"]), 3),
        }
    ok, detail = _probe_card(timeout)
    _CARD_CACHE.update({"at": time.monotonic(), "up": ok, "detail": detail})
    return {"ok": ok, "detail": detail, "cached": False, "age_s": 0.0}


def reset_card_cache() -> None:
    """Tests and the shutdown path start from "unknown"."""
    _CARD_CACHE.update({"at": 0.0, "up": False, "detail": "not probed yet"})


# --- the routes -------------------------------------------------------------


@router.get("/healthz")
def healthz() -> dict:
    """Liveness: no I/O, no probe, no lock. Always cheap and always 200."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz():
    """Readiness: volume, database, crew server. 503 when the volume is unusable.

    A ``def`` handler on purpose: FastAPI runs it in the threadpool, so the
    blocking (but timeout-bounded and cached) card probe cannot block the loop.
    """
    writable, write_detail = volume_writable()
    db_ok, db_detail = database_openable()
    crew = crew_state()

    ready = writable and db_ok
    body = {
        "status": "ready" if ready else "not-ready",
        "degraded": not crew["ok"],
        **version_info(),
        "checks": {
            "volume": {"ok": writable, "path": str(data_dir()), "detail": write_detail},
            "database": {"ok": db_ok, "path": str(events_db()), "detail": db_detail},
            "crew_server": crew,
        },
    }
    return JSONResponse(body, status_code=200 if ready else 503)


# --- lifecycle --------------------------------------------------------------


def prepare_runtime() -> dict:
    """Create the directories the app and a run need; verify the volume.

    Never raises: a broken volume must not stop the process from starting. It
    is reported by ``/readyz`` (and logged here) instead, so the platform can
    hold traffic back rather than crash-loop the container.
    """
    created: list[str] = []
    failures: list[str] = []
    for path in (
        data_dir(),
        data_dir() / "settings",
        data_dir() / "tmp",
        Path(".run"),
        Path(".run/runs"),
        Path(".run/api"),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path))
        except OSError as exc:
            failures.append(f"{path}: {type(exc).__name__}: {exc}")

    writable, detail = volume_writable()
    if not writable:
        failures.append(f"volume not writable: {detail}")
    # TMPDIR/TEMP/TMP are set in the image (Dockerfile) so Chroma, matplotlib and
    # pydantic's temp files land on the volume; they are not touched here, so
    # this helper has no side effects outside the directories it creates.

    ok = not failures
    if ok:
        log.info("startup: runtime directories ready", extra={"event": "startup"})
    else:
        log.error(
            "startup: runtime directories are not usable: %s",
            "; ".join(failures),
            extra={"event": "startup"},
        )
    reset_card_cache()
    return {"ok": ok, "created": created, "failures": failures}


def crew_pid_file() -> Path:
    return Path(os.getenv("ATHENA_CREW_PID_FILE") or ".run/crew.pid")


def crew_stop_file() -> Path:
    return Path(os.getenv("ATHENA_CREW_STOP_FILE") or ".run/crew.stop")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_crew_server(timeout: float = SHUTDOWN_TIMEOUT_S) -> bool:
    """Stop the supervised crew server the container entrypoint started.

    The entrypoint writes its pid to ``.run/crew.pid``. A stop file is created
    first so the supervisor does not restart it, then SIGTERM, then (after the
    grace period) SIGKILL. Returns True when nothing is left running. Always
    safe to call: a bare checkout has no pid file and this is a no-op.
    """
    stop_file = crew_stop_file()
    try:
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("stop\n", encoding="utf-8")
    except OSError:
        pass

    path = crew_pid_file()
    try:
        pid = int((path.read_text(encoding="utf-8") or "").strip())
    except (OSError, ValueError):
        return True
    if pid <= 0 or not _alive(pid):
        return True

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return not _alive(pid)


def build_lifespan(registry=None):
    """Startup/shutdown hooks for ``create_app``.

    Shutdown order matters: stop the run subprocess first (it spends tokens),
    then the child crew server (it is the run's only A2A dependency).
    """

    @asynccontextmanager
    async def lifespan(_app):  # noqa: ANN001 - FastAPI hook
        state = prepare_runtime()
        log.info(
            "athena api started version=%s commit=%s data_dir=%s",
            version_info()["version"],
            version_info()["commit"],
            str(data_dir()),
            extra={"event": "startup", "ok": state["ok"]},
        )
        try:
            yield
        finally:
            await _shutdown(registry)

    return lifespan


async def _shutdown(registry) -> None:
    log.info("shutdown: stopping the active run", extra={"event": "shutdown"})
    if registry is not None and hasattr(registry, "terminate_all"):
        try:
            stopped = await asyncio.to_thread(registry.terminate_all, SHUTDOWN_TIMEOUT_S)
            log.info(
                "shutdown: run subprocess %s",
                "stopped" if stopped else "did not stop in time",
                extra={"event": "shutdown"},
            )
        except Exception:  # noqa: BLE001 - shutdown must never raise
            log.exception("shutdown: terminating the active run failed")
    try:
        crew_stopped = await asyncio.to_thread(stop_crew_server, SHUTDOWN_TIMEOUT_S)
        log.info(
            "shutdown: crew server %s",
            "stopped" if crew_stopped else "did not stop in time",
            extra={"event": "shutdown"},
        )
    except Exception:  # noqa: BLE001
        log.exception("shutdown: stopping the crew server failed")


# --- logging ----------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per line: readable by fly/render/CloudWatch collectors."""

    _EXTRA = ("event", "run_id", "method", "path", "status", "ok", "duration_ms")

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name in self._EXTRA:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Structurally log everything the app logs.

    ``logging.basicConfig`` is deliberately non-forcing: under pytest, uvicorn
    or gunicorn the root logger already has a handler and this is a no-op, so no
    existing capture (or the platform's own log line) is replaced.
    """
    level_name = (os.getenv("ATHENA_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    style = (os.getenv("ATHENA_LOG_FORMAT") or "json").strip().lower()

    handler = logging.StreamHandler()
    if style == "text":
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    else:
        handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=False)
