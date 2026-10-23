"""Cross-process sliding-window limiter for LLM input tokens.

Free tiers meter *input tokens per minute* across the whole account, so the ADK
process and the CrewAI server share one budget. The window lives in SQLite, so
both processes see the same numbers instead of each politely staying under a
limit that the pair still blows through.

Clock and sleep are injectable so the tests do not wait.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Callable

WINDOW_SECONDS = 60.0
BUSY_TIMEOUT_MS = 30_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    tokens INTEGER NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts);
"""


class RateLimitWaitError(RuntimeError):
    """Raised when room cannot be made within the allowed wait."""


def default_db_path() -> Path:
    return Path(os.getenv("RATELIMIT_DB") or Path(".run") / "ratelimit.db")


def estimate_tokens(text: str) -> int:
    """Rough token estimate: about four characters per token."""
    return max(1, len(text) // 4)


class TokenLimiter:
    """Blocks until ``estimated`` more tokens fit inside the sliding window."""

    def __init__(
        self,
        path: str | Path | None = None,
        limit: int | None = None,
        max_wait_s: float | None = None,
        window: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = str(path or default_db_path())
        self.limit = int(limit if limit is not None else os.getenv("LLM_ITPM_LIMIT") or 0)
        self.max_wait_s = float(
            max_wait_s if max_wait_s is not None
            else os.getenv("LLM_RATELIMIT_MAX_WAIT_S") or 120
        )
        self.window = window
        self._clock = clock
        self._sleep = sleep
        self._last_id: int | None = None

        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    def _used(self, now: float) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) AS total FROM calls WHERE ts > ?",
                (now - self.window,),
            ).fetchone()
            return int(row["total"])
        finally:
            conn.close()

    def _oldest(self, now: float) -> float | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT MIN(ts) AS first FROM calls WHERE ts > ?", (now - self.window,)
            ).fetchone()
            return float(row["first"]) if row["first"] is not None else None
        finally:
            conn.close()

    def _insert(self, tokens: int, now: float) -> int:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "INSERT INTO calls (ts, tokens) VALUES (?, ?)", (now, tokens)
            )
            conn.execute(
                "DELETE FROM calls WHERE ts < ?", (now - 10 * self.window,)
            )
            conn.execute("COMMIT")
            return int(cursor.lastrowid)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def acquire(self, estimated_tokens: int) -> None:
        """Reserve budget, waiting for the window to free space if needed."""
        estimate = max(0, int(estimated_tokens))
        if self.limit <= 0:
            self._last_id = self._insert(estimate, self._clock())
            return

        deadline = self._clock() + self.max_wait_s
        while True:
            now = self._clock()
            conn = self._connect()
            try:
                # Check and insert in one write transaction: two processes must
                # not both see room that only exists for one of them.
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT COALESCE(SUM(tokens), 0) AS total, MIN(ts) AS first"
                    " FROM calls WHERE ts > ?",
                    (now - self.window,),
                ).fetchone()
                used = int(row["total"])
                oldest = row["first"]
                # An oversized request is allowed once the window is otherwise
                # empty, so a single large prompt can never deadlock.
                if (used == 0 and estimate > self.limit) or used + estimate <= self.limit:
                    cursor = conn.execute(
                        "INSERT INTO calls (ts, tokens) VALUES (?, ?)", (now, estimate)
                    )
                    conn.execute("DELETE FROM calls WHERE ts < ?", (now - 10 * self.window,))
                    conn.execute("COMMIT")
                    self._last_id = int(cursor.lastrowid)
                    return
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

            wait = max(0.05, (oldest + self.window - now) if oldest else 0.05)
            if now + wait > deadline:
                raise RateLimitWaitError(
                    f"would wait {wait:.1f}s for {estimate} tokens "
                    f"(limit {self.limit}/min, used {used}); "
                    f"max wait is {self.max_wait_s:.0f}s"
                )
            self._sleep(wait)

    def record(self, actual_tokens: int) -> None:
        """Replace this process's last estimate with the real usage."""
        if self._last_id is None:
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE calls SET tokens = ?, settled = 1 WHERE id = ?",
                (max(0, int(actual_tokens)), self._last_id),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        self._last_id = None


_LIMITER: TokenLimiter | None = None


def get_limiter() -> TokenLimiter:
    """The process-wide limiter, configured from the environment."""
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = TokenLimiter()
    return _LIMITER


def reset_limiter(limiter: TokenLimiter | None = None) -> None:
    """Swap the process-wide limiter (tests and the runner use this)."""
    global _LIMITER
    _LIMITER = limiter

_OUT_LIMITER: TokenLimiter | None = None


def get_output_limiter() -> TokenLimiter:
    """Second bucket for output tokens: providers meter request max_tokens too.

    Groq's free tier allows 1000 output tokens per minute, so a 600-token
    researcher call and a 1500-token writer call cannot both land in one window.
    """
    global _OUT_LIMITER
    if _OUT_LIMITER is None:
        _OUT_LIMITER = TokenLimiter(
            path=os.getenv("RATELIMIT_OUT_DB") or Path(".run") / "ratelimit_out.db",
            limit=int(os.getenv("LLM_OTPM_LIMIT") or 0),
        )
    return _OUT_LIMITER


def reset_limiters(limiter: TokenLimiter | None = None, output: TokenLimiter | None = None) -> None:
    """Swap both limiters (tests and the runner use this)."""
    global _LIMITER, _OUT_LIMITER
    _LIMITER = limiter
    _OUT_LIMITER = output