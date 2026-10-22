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
