"""Housekeeping: secrets and scratch files must never be committable."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

MUST_BE_IGNORED = [
    "data/settings/master.key",
    "data/settings/secrets.enc.json",
    "data/settings/profiles.json",
    "data/settings/active_profile.json",
    "data/user_docs/example.pdf",
    "data/chroma/example",
    ".run/run.log",
    "record1.txt",
    "run_seq.txt",
    "report5.txt",
]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_paths_are_ignored(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", path],
        cwd=str(ROOT), capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"{path} is not ignored (it could be committed)"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_no_vault_file_is_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True, check=False,
    ).stdout.splitlines()

    leaks = [name for name in tracked
             if name.endswith(("master.key", "secrets.enc.json", "profiles.json"))]
    assert leaks == [], f"tracked vault files: {leaks}"


def test_gitignore_has_no_merged_lines() -> None:
    """A missing newline once merged two patterns into one useless line."""
    for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines():
        if line.startswith("data/"):
            assert "/" in line[5:] or line.count("data/") == 1, f"malformed entry: {line}"
