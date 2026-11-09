"""Cross-process single-flight lock for one evaluation results directory."""

from __future__ import annotations

import errno
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path


class OutputDirectoryBusyError(RuntimeError):
    """Another process owns this results directory."""


class OutputDirectoryLock:
    """Advisory OS lock; the persistent file is never unlinked."""

    def __init__(self, out_dir: str | Path) -> None:
        self.out_dir = Path(out_dir).resolve()
        self.path = self.out_dir / ".evaluation.lock"
        self._handle = None
        self._windows = os.name == "nt"

    def __enter__(self) -> "OutputDirectoryLock":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self._handle = os.fdopen(fd, "r+b", buffering=0)
        try:
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            self._lock()
        except (BlockingIOError, OSError) as exc:
            if not self._is_busy_error(exc):
                self._handle.close()
                self._handle = None
                raise
            owner = self._read_owner()
            self._handle.close()
            self._handle = None
            raise OutputDirectoryBusyError(
                f"results directory is busy: {self.out_dir}; {owner}"
            ) from None

        try:
            owner = {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "out_dir": str(self.out_dir),
            }
            # Byte zero is reserved for the OS lock; keep owner metadata readable
            # while Windows holds its mandatory byte-range lock.
            self._handle.seek(1)
            self._handle.truncate(1)
            self._handle.write(json.dumps(owner, sort_keys=True).encode("utf-8"))
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except BaseException:
            try:
                self._unlock()
            except OSError:
                pass
            finally:
                self._handle.close()
                self._handle = None
            raise
        return self

    def __exit__(self, *_exc_info) -> None:
        try:
            self._unlock()
        finally:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    def _lock(self) -> None:
        self._handle.seek(0)
        if self._windows:
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        if self._handle is None:
            return
        self._handle.seek(0)
        if self._windows:
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)

    def _read_owner(self) -> str:
        try:
            self._handle.seek(1)
            raw = self._handle.read().decode("utf-8")
            owner = json.loads(raw)
            return (
                f"owner pid={owner['pid']} host={owner['hostname']} "
                f"started={owner['started_at']} out_dir={owner['out_dir']}"
            )
        except (KeyError, OSError, UnicodeError, ValueError, TypeError):
            return "owner metadata unavailable"

    def _is_busy_error(self, exc: OSError) -> bool:
        if self._windows:
            deadlock = getattr(errno, "EDEADLOCK", errno.EDEADLK)
            return exc.errno in {errno.EACCES, deadlock, errno.EAGAIN}
        return exc.errno in {errno.EACCES, errno.EAGAIN}
