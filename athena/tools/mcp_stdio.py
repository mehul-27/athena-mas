"""A stable stderr sink for MCP stdio tool servers.

MCP's stdio client takes ``errlog: TextIO = sys.stderr`` — the value is captured
when :mod:`mcp.client.stdio` is imported — and passes that object straight to
``subprocess`` as the child's stderr. On Windows ``subprocess`` resolves it with
``msvcrt.get_osfhandle(stderr.fileno())``, so if ``sys.stderr`` was replaced by
``contextlib.redirect_stderr(<log file>)`` and that file is later closed, every
attempt to start a tool server dies with ``ValueError: I/O operation on closed
file`` before the child process exists. The toolset then fails to load, ADK
drops it, and the researcher silently runs with no tools at all.

That is exactly what the in-process evaluation backend did: it wrapped a whole
run in ``redirect_stdout``/``redirect_stderr`` to a per-run log file. Every tool
server vanished and every pattern scored zero evidence.

The fix is to never hand MCP a process-wide stream. This module owns the object
we pass instead — a real file with a real ``fileno()``, opened once per process
and never closed, so redirection, a closed log file, or a replaced ``sys.stderr``
cannot invalidate it. ``os.devnull`` and ``sys.__stderr__`` are the fallbacks,
in that order, and each is only accepted after its handle is proven usable.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import TextIO

from athena.tools.settings import data_dir

DEFAULT_LOG_NAME = "mcp_servers.log"

_lock = threading.Lock()
_sink: TextIO | None = None


def log_path() -> Path:
    """Where tool-server stderr is appended (``ATHENA_MCP_STDERR_LOG`` wins)."""
    override = (os.getenv("ATHENA_MCP_STDERR_LOG") or "").strip()
    if override:
        return Path(override)
    return data_dir() / "logs" / DEFAULT_LOG_NAME


def usable(stream: object) -> bool:
    """True when ``stream`` is an open file object with a working OS handle.

    A ``StringIO`` has no ``fileno``, and a redirected-then-closed log file has
    a ``fileno`` that raises — both are rejected here rather than being handed
    to ``subprocess`` and failing inside the child spawn.
    """
    if stream is None or getattr(stream, "closed", True):
        return False
    try:
        handle = stream.fileno()  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False
    try:
        os.fstat(handle)
    except (OSError, ValueError):
        return False
    return True


def _open_log() -> TextIO | None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return None


def _open_devnull() -> TextIO | None:
    try:
        return open(os.devnull, "a", encoding="utf-8")
    except OSError:  # pragma: no cover - devnull always opens when anything does
        return None


def stderr_sink() -> TextIO | None:
    """The never-closed stream to give MCP as ``errlog``.

    Opened once per process and reused, so a long run does not leak handles.
    Returns ``None`` only when no usable stream exists at all; callers must then
    fall back to whatever they were going to pass anyway.
    """
    global _sink
    with _lock:
        if usable(_sink):
            return _sink
        for candidate in (_open_log(), _open_devnull(), sys.__stderr__):
            if usable(candidate):
                _sink = candidate  # type: ignore[assignment]
                return _sink
        return None


def toolset_kwargs() -> dict[str, TextIO]:
    """Keyword arguments that make an ``McpToolset`` immune to redirection.

    ``MCPToolset.__init__`` declares ``errlog: TextIO = sys.stderr``, so the
    default is whatever ``sys.stderr`` was *at ADK import time* — a log file the
    evaluation backend had redirected and then closed. Passing our own sink
    explicitly replaces that captured default with a handle that outlives any
    ``redirect_stdout``/``redirect_stderr`` block and is never closed.

    When nothing usable exists (no writable log, no ``/dev/null``, no original
    stderr) this returns ``{}`` so the caller keeps ADK's default rather than
    handing ``errlog=None`` to ``stdio_client``, which would silently inherit the
    parent's stderr instead of capturing it.
    """
    sink = stderr_sink()
    return {"errlog": sink} if sink is not None else {}


def reset_stderr_sink() -> None:
    """Drop the cached sink (tests only; leaves the file itself open)."""
    global _sink
    with _lock:
        _sink = None


__all__ = [
    "log_path",
    "reset_stderr_sink",
    "stderr_sink",
    "toolset_kwargs",
    "usable",
]
