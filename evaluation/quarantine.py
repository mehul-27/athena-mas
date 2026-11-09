"""Fail-closed checks for evaluation batches marked as quarantined."""

from __future__ import annotations

import os
from pathlib import Path

MARKER_NAME = "QUARANTINE.json"


class QuarantinedPathError(RuntimeError):
    """Raised when a path is inside or cannot be distinguished from quarantine."""


def _marker_in_ancestors(path: Path) -> Path | None:
    try:
        candidate = Path(path).expanduser()
        lexical = Path(os.path.abspath(candidate))
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise QuarantinedPathError(f"cannot safely resolve evaluation path {path}") from exc
    for base in dict.fromkeys((lexical, resolved)):
        for directory in (base, *base.parents):
            marker = directory / MARKER_NAME
            try:
                marker.lstat()
            except FileNotFoundError:
                continue
            except NotADirectoryError:
                continue
            except OSError as exc:
                raise QuarantinedPathError(
                    f"cannot safely check quarantine marker {marker}"
                ) from exc
            return marker
    return None


def is_quarantined(path: Path) -> bool:
    """Return true for marked paths, treating access/resolve errors as blocked."""
    try:
        return _marker_in_ancestors(path) is not None
    except QuarantinedPathError:
        return True


def assert_not_quarantined(path: Path) -> None:
    """Raise before reading, resuming, or writing an evaluation batch."""
    marker = _marker_in_ancestors(path)
    if marker is not None:
        raise QuarantinedPathError(
            f"evaluation path {Path(path).expanduser().resolve(strict=False)} is quarantined "
            f"by {marker}"
        )
