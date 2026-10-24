"""Test configuration.

Replaces pytest's ``tmp_path`` with a repo-local equivalent. pytest creates its
temporary directories with permissions 0700, which keeps them private to the
creating token; in a sandboxed/restricted Windows session that makes them
unreadable again on the next access, including by pytest's own cleanup. This
fixture gives each test a unique directory with inherited permissions instead.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

_TMP_BASE = Path(__file__).resolve().parent.parent / ".test-tmp"


@pytest.fixture()
def tmp_path() -> Path:
    """A unique, writable directory for one test."""
    path = _TMP_BASE / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    return path

@pytest.fixture(autouse=True)
def _isolated_databases(tmp_path, monkeypatch):
    """Keep every test off the real data/ and .run/ databases.

    Without this a stub demo run can write into data/events.db, which is exactly
    what happened before Step 6c. Paths are per-test, and the cached limiters are
    reset so they pick the temporary files up.
    """
    from athena.config import ratelimit

    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("BLACKBOARD_DB", str(tmp_path / "blackboard.db"))
    monkeypatch.setenv("RATELIMIT_DB", str(tmp_path / "ratelimit.db"))
    monkeypatch.setenv("RATELIMIT_OUT_DB", str(tmp_path / "ratelimit_out.db"))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "mpl"))
    ratelimit.reset_limiters()
    yield
    ratelimit.reset_limiters()