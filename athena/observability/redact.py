"""Redaction for event text.

Two passes: exact values of secret-looking environment variables (compared in
memory, never printed), then a pattern pass for token-shaped strings and bearer
headers. The result is what gets stored.
"""

from __future__ import annotations

import os
import re

SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
REDACTED = "[REDACTED]"

_TOKEN_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|gsk|pk|rk|xox[abprs])[-_][\w-]{8,}"),
    re.compile(r"(?i)\bBearer\s+[\w.\-]{8,}"),
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),
)


def secret_values() -> list[str]:
    """Values of environment variables whose names look secret. Never logged."""
    values = []
    for name, value in os.environ.items():
        if len(value) < 8:
            continue
        if any(marker in name.upper() for marker in SECRET_MARKERS):
            values.append(value)
    # Longest first so a short secret cannot leave a fragment of a longer one.
    return sorted(values, key=len, reverse=True)


def redact(text: str | None) -> str | None:
    """Replace secrets and token-shaped strings with ``[REDACTED]``."""
    if not text:
        return text
    cleaned = text
    for value in secret_values():
        if value in cleaned:
            cleaned = cleaned.replace(value, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned
