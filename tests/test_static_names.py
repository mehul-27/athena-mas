"""Static check: no undefined names anywhere in athena/, scripts/ or tests/.

This is the test that would have caught `_parts` after the Step 6c refactor:
pyflakes reports a name that is used but never defined or imported. Unused
imports are ignored here — they are style, not breakage.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pyflakes.api
import pyflakes.reporter

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [ROOT / "athena", ROOT / "scripts", ROOT / "tests"]


class _Collector(pyflakes.reporter.Reporter):
    """Reporter that keeps the messages instead of printing them."""

    def __init__(self) -> None:
        super().__init__(sys.stderr, sys.stderr)
        self.messages: list[str] = []

    def flake(self, message) -> None:  # noqa: D102 - pyflakes interface
        self.messages.append(str(message))

    def unexpectedError(self, filename, message) -> None:  # noqa: N802
        self.messages.append(f"{filename}: {message}")

    def syntaxError(self, filename, msg, lineno, offset, text) -> None:  # noqa: N802
        self.messages.append(f"{filename}:{lineno}: {msg}")


def _run() -> list[str]:
    collector = _Collector()
    for target in TARGETS:
        pyflakes.api.checkRecursive([str(target)], collector)
    return collector.messages


def test_no_undefined_names() -> None:
    undefined = [message for message in _run() if "undefined name" in message]
    assert not undefined, "undefined names:\n" + "\n".join(undefined)


def test_no_syntax_errors() -> None:
    broken = [
        message for message in _run()
        if "invalid syntax" in message or "unexpected indent" in message
    ]
    assert not broken, "syntax errors:\n" + "\n".join(broken)


def test_pyflakes_actually_ran() -> None:
    """A silent zero-finding result must mean 'clean', not 'nothing scanned'."""
    files = list(pyflakes.api.iterSourceCode([str(ROOT / "athena")]))
    assert len(files) > 20, f"only {len(files)} files were discoverable"
