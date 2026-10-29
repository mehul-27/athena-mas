"""Running the preflight probe without blocking the API.

``subprocess.run`` inside a request handler holds a worker thread for up to the
whole timeout. Six clicks on "Test connection" therefore hung six requests with
no HTTP status while ``/api/health`` stayed responsive — the single-flight lock
and the async child process in this module are the fix.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

LINE_LIMIT = 200
# Tests replace this with a stub command; production runs the real preflight.
PREFLIGHT_ARGV = [sys.executable, "scripts/preflight.py", "--live", "--json"]
_lock = asyncio.Lock()
_running: set[str] = set()

log = logging.getLogger("athena.settings")


class Busy(RuntimeError):
    """A test for this profile is already running."""


def _child_env(env: dict[str, str]) -> dict[str, str]:
    child = dict(os.environ)
    child.update(env)
    return child


def _kill_tree(process) -> None:
    """Windows needs the whole tree; posix can settle for the child."""
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            import subprocess

            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           capture_output=True, check=False)
        else:
            process.kill()
    except Exception:  # noqa: BLE001 - killing is best effort
        pass


def _error(kind: str, message: str) -> list[dict]:
    return [{"path": "preflight", "ok": False, "error_type": kind,
             "error": message[:LINE_LIMIT]}]


def _parse_json_output(text: str):
    """Return a JSON array/object even if a probe logged a line first.

    The child process owns stdout, but a probe may legitimately emit a
    human-readable diagnostic before the JSON body. Only a complete JSON value
    that reaches the end of the stream is accepted; malformed or truncated
    payloads still fall through to ``InvalidJson``.
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(stripped[index:])
        except ValueError:
            continue
        if isinstance(value, (list, dict)) and not stripped[index + end:].strip():
            return value
    return None


async def run_preflight(env: dict[str, str], timeout: int = 120) -> list[dict]:
    """One preflight call as an async child process. Always returns a list."""
    started = time.perf_counter()
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *PREFLIGHT_ARGV,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_child_env(env),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if process is not None:
            _kill_tree(process)
        log.warning("preflight timeout after %ss", timeout)
        return _error("TimeoutExpired", f"preflight did not finish within {timeout}s")
    except Exception as exc:  # noqa: BLE001 - a broken probe is a result, not a crash
        log.warning("preflight failed: %s", type(exc).__name__)
        return _error(type(exc).__name__, str(exc))
    finally:
        if process is not None and process.returncode is None:
            _kill_tree(process)

    log.info("preflight finished in %.1fs (exit %s)",
             time.perf_counter() - started, process.returncode)
    parsed = _parse_json_output(stdout.decode("utf-8", "replace"))
    if parsed is None:
        text = (stderr or stdout).decode("utf-8", "replace")
        return _error("InvalidJson", text.strip() or "preflight printed nothing")
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return _error("InvalidJson", "preflight did not print a JSON list")


async def test_profile(profile_id: str, env: dict[str, str], timeout: int = 120) -> dict:
    """Single-flight wrapper: one test per profile, logged start/finish."""
    if profile_id in _running:
        raise Busy("a test is already running for this provider")
    _running.add(profile_id)
    started = time.perf_counter()
    log.info("test start profile=%s", profile_id)
    try:
        results = await run_preflight(env, timeout=timeout)
    finally:
        _running.discard(profile_id)
        log.info("test finish profile=%s in %.1fs",
                 profile_id, time.perf_counter() - started)
    failed = [row for row in results if not row.get("ok")]
    if failed:
        log.info("test errors profile=%s: %s", profile_id,
                 failed[0].get("error_type", "unknown"))
    return {"results": results}
