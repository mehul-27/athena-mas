"""Secret detection and redaction for strings and nested structures.

:mod:`athena.observability.redact` remains the single low-level redactor for
event text: it knows the live environment's secret values and a few token
shapes. This module adds the shapes that actually leak in this application —
PEM private keys, JWTs, provider key prefixes, ``Authorization`` headers — and,
most importantly, a recursive pass over an arbitrary structure so a stored chat
message, a tool payload or an API section is redacted field by field instead of
only when someone remembers to wrap one string.

``redact_tree`` is total: it never raises, preserves the container types it
knows, and copies pydantic models rather than mutating them.
"""

from __future__ import annotations

import re
from typing import Any

from athena.observability.redact import REDACTED, redact as redact_event_text

MAX_DEPTH = 12

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private keys, including OPENSSH and RSA/EC/DSA variants.
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
    # JSON Web Tokens: header.payload.signature, base64url, header starts eyJ.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}\b"), REDACTED),
    # Provider key prefixes.
    (
        re.compile(
            r"\b(?:sk|sk-proj|sk-ant|gsk|xox[abprs]|ghp|gho|ghs|ghu|ghr|github_pat"
            r"|shpat|shpss|pk|rk|hf|api)[-_][A-Za-z0-9_\-]{12,}\b"
        ),
        REDACTED,
    ),
    # AWS access key id and secret.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (
        re.compile(
            r"(?i)\b(?:aws_secret_access_key|aws_session_token)\b[\"']?\s*[:=]\s*[\"']?"
            r"[A-Za-z0-9/+=]{16,}"
        ),
        REDACTED,
    ),
    # Authorization / api-key headers, keeping the field name for readability.
    (
        re.compile(
            r"(?i)\b(authorization|proxy-authorization|x-api-key|api[-_]?key"
            r"|access[-_]?token|refresh[-_]?token|client[-_]?secret)\b"
            r"([\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9_\-./+~=]{8,}"
        ),
        r"\1\2" + REDACTED,
    ),
    # Bearer tokens anywhere.
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]{8,}=*"), r"\1 " + REDACTED),
    # Credentials embedded in a URL.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTED + "@"),
    # key=value pairs where the value looks like a secret in a query string.
    (
        re.compile(
            r"(?i)([?&](?:key|api_key|apikey|token|secret|password|sig|signature)=)"
            r"[^&\s#]{6,}"
        ),
        r"\1" + REDACTED,
    ),
)


def redact_text(text: str | None) -> str | None:
    """Redact known secret shapes, then hand off to the event redactor."""
    if not text:
        return text
    cleaned = str(text)
    for pattern, replacement in _PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return redact_event_text(cleaned)


def contains_secret(text: str | None) -> bool:
    """True when ``text`` carries a secret-shaped string."""
    if not text:
        return False
    body = str(text)
    if any(pattern.search(body) for pattern, _ in _PATTERNS):
        return True
    return (redact_event_text(body) or "") != body


def secret_kinds(text: str | None) -> list[str]:
    """Rule names for the shapes found, for a readable guardrail detail."""
    if not text:
        return []
    body = str(text)
    found: list[str] = []
    labels = (
        "pem_private_key",
        "jwt",
        "provider_key",
        "aws_key_id",
        "aws_secret",
        "auth_header",
        "bearer_token",
        "url_credentials",
        "query_secret",
    )
    for label, (pattern, _replacement) in zip(labels, _PATTERNS):
        if pattern.search(body):
            found.append(label)
    return found


def redact_tree(obj: Any, _depth: int = 0) -> Any:
    """Redact every string inside ``obj``, recursively, without mutating it."""
    if _depth > MAX_DEPTH:
        return obj
    try:
        if isinstance(obj, str):
            return redact_text(obj)
        if isinstance(obj, bytes):
            try:
                return redact_text(obj.decode("utf-8", "replace")).encode("utf-8")  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 - bytes are best effort
                return obj
        if isinstance(obj, dict):
            return {
                redact_tree(key, _depth + 1): redact_tree(value, _depth + 1)
                for key, value in obj.items()
            }
        if isinstance(obj, (list, tuple, set, frozenset)):
            items = [redact_tree(item, _depth + 1) for item in obj]
            return type(obj)(items) if not isinstance(obj, frozenset) else frozenset(items)
        model_copy = getattr(obj, "model_copy", None)
        fields = getattr(type(obj), "model_fields", None)
        if callable(model_copy) and isinstance(fields, dict):
            updates = {
                name: redact_tree(getattr(obj, name, None), _depth + 1) for name in fields
            }
            return model_copy(update=updates)
        return obj
    except Exception:  # noqa: BLE001 - redaction must never break a caller
        return obj
