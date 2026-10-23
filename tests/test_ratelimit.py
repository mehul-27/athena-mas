"""Offline tests for the shared token limiter (fake clock, no real waiting)."""

from __future__ import annotations

import multiprocessing
import sqlite3

import pytest

from athena.config.ratelimit import RateLimitWaitError, TokenLimiter


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _limiter(tmp_path, clock, limit=1000, max_wait=120, window=60):
    return TokenLimiter(
        tmp_path / "rl.db", limit=limit, max_wait_s=max_wait,
        window=window, clock=clock, sleep=clock.sleep,
    )


def _used(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT COALESCE(SUM(tokens),0) FROM calls").fetchone()[0])
    finally:
        conn.close()


def test_under_limit_does_not_wait(tmp_path) -> None:
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock)

    limiter.acquire(100)
    limiter.acquire(200)

    assert clock.now == 1000.0
    assert _used(tmp_path / "rl.db") == 300


def test_over_limit_waits_for_the_window(tmp_path) -> None:
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock, limit=1000)

    limiter.acquire(800)
    limiter.acquire(400)  # 800 + 400 > 1000: must wait for the first to expire

    assert clock.now >= 1060.0
    assert _used(tmp_path / "rl.db") == 1200


def test_exceeding_max_wait_raises(tmp_path) -> None:
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock, limit=100, max_wait=5)

    limiter.acquire(100)
    with pytest.raises(RateLimitWaitError) as excinfo:
        limiter.acquire(50)

    assert "max wait" in str(excinfo.value)


def test_oversized_request_does_not_deadlock(tmp_path) -> None:
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock, limit=100)

    limiter.acquire(500)  # bigger than the whole budget, but the window is empty

    assert clock.now == 1000.0


def test_record_replaces_the_estimate(tmp_path) -> None:
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock)

    limiter.acquire(900)
    limiter.record(10)

    assert _used(tmp_path / "rl.db") == 10


def test_disabled_limit_never_waits(tmp_path) -> None:
    clock = FakeClock()
    limiter = _limiter(tmp_path, clock, limit=0)

    for _ in range(5):
        limiter.acquire(10_000)

    assert clock.now == 1000.0


def _worker(db_path: str, out_path: str) -> None:
    """Two of these share one DB; the second must wait for the first."""
    clock = FakeClock()
    limiter = TokenLimiter(db_path, limit=100, window=60, clock=clock, sleep=clock.sleep)
    limiter.acquire(60)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(str(clock.now))


def test_two_processes_share_one_window(tmp_path) -> None:
    db_path = str(tmp_path / "shared.db")
    TokenLimiter(db_path, limit=100, clock=FakeClock(), sleep=lambda s: None).acquire(0)

    context = multiprocessing.get_context("spawn")
    outs = [str(tmp_path / f"out{index}.txt") for index in range(2)]
    processes = [
        context.Process(target=_worker, args=(db_path, out)) for out in outs
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=120)

    assert [process.exitcode for process in processes] == [0, 0]
    stamps = sorted(float(open(path, encoding="utf-8").read()) for path in outs)
    # 60 + 60 > 100, so the second process had to wait a whole window.
    assert stamps[1] - stamps[0] >= 60
