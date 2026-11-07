"""Validation of a tool result before it reaches the model or the blackboard.

A tool is outside the trust boundary twice over: it fetches third-party text and
it may return whatever the remote end felt like sending. This validator answers
one question — "is this safe to hand on as data?" — and never raises, because a
failed validation must degrade to a flagged result, not a crashed run.

Checks, each an independent flag:

* byte cap (``ATHENA_GUARDRAILS_TOOL_MAX_BYTES``, default 64 KiB)
* text sanity (undecodable bytes, lone surrogates, replacement-character flood,
  control characters)
* URL sanity (scheme must be http/https, host must not be a private, loopback or
  link-local address unless explicitly allowed)
* embedded instructions (the same lexical layer as user input: an instruction
  inside a page is still not an instruction)
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from athena.blackboard.schemas import GuardrailFlag
from athena.guardrails import env
from athena.guardrails.injection import scan_injection

STAGE = "tool_output"

ALLOWED_SCHEMES = frozenset({"http", "https"})
_PRIVATE_HOST_SUFFIXES = (
    ".internal",
    ".local",
    ".localhost",
    ".localdomain",
    ".home.arpa",
)
_PRIVATE_HOSTS = frozenset({"localhost", "ip6-localhost", "metadata.google.internal"})
# ``scheme://rest`` (the only shape a fetcher should act on) and the
# non-network schemes that are interesting precisely because nothing fetches them.
_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{1,15})://[^\s<>\"'`]{0,2048}")
_DANGEROUS_SCHEME = re.compile(
    r"(?i)\b(javascript|data|vbscript|file|blob|about|jar|chrome|ms-msdt):[^\s<>\"'`]{1,2048}"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REPLACEMENT_FLOOD = 0.05


def _flag(rule: str, action: str, detail: str) -> GuardrailFlag:
    return GuardrailFlag(stage=STAGE, rule=rule, action=action, detail=detail[:300])


def _as_text(payload: Any) -> tuple[str, list[GuardrailFlag]]:
    """Serialise a payload to text, reporting anything that will not encode."""
    flags: list[GuardrailFlag] = []
    if isinstance(payload, str):
        return payload, flags
    try:
        return json.dumps(payload, ensure_ascii=False, default=str), flags
    except (TypeError, ValueError) as exc:
        flags.append(
            _flag("tool_output.unserialisable", "sanitized",
                  f"{type(exc).__name__}: payload could not be serialised")
        )
        try:
            return str(payload), flags
        except Exception:  # noqa: BLE001 - a broken __str__ is still a report
            return "", flags


def _private_host(host: str) -> bool:
    name = (host or "").strip().lower().rstrip(".")
    if not name:
        return False
    if name in _PRIVATE_HOSTS:
        return True
    if name.endswith(_PRIVATE_HOST_SUFFIXES):
        return True
    if name in {"::1", "0.0.0.0", "[::1]"}:
        return True
    parts = name.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        first, second = int(parts[0]), int(parts[1])
        if first == 127 or first == 10 or first == 0:
            return True
        if first == 192 and second == 168:
            return True
        if first == 172 and 16 <= second <= 31:
            return True
        if first == 169 and second == 254:
            return True
        if first == 100 and 64 <= second <= 127:
            return True
    return False


def _url_flags(text: str) -> list[GuardrailFlag]:
    flags: list[GuardrailFlag] = []
    seen: set[str] = set()
    for match in _URL.finditer(text):
        raw = match.group(0)
        scheme = (match.group(1) or "").lower()
        if scheme not in ALLOWED_SCHEMES:
            if raw in seen:
                continue
            seen.add(raw)
            flags.append(
                _flag("tool_output.url_scheme", "sanitized",
                      f"unexpected URL scheme {scheme!r}: {raw[:120]}")
            )
            continue
        host = urlparse(raw).hostname or ""
        if host and not env.tool_allow_local_hosts() and _private_host(host):
            if raw in seen:
                continue
            seen.add(raw)
            flags.append(
                _flag("tool_output.url_host", "sanitized",
                      f"non-public host {host!r} in tool output")
            )
    for match in _DANGEROUS_SCHEME.finditer(text):
        raw = match.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        flags.append(
            _flag("tool_output.url_scheme", "sanitized",
                  f"active-content URL scheme: {raw[:120]}")
        )
    return flags


def validate_tool_output(
    tool_name: str,
    payload: Any,
    *,
    max_bytes: int | None = None,
) -> tuple[bool, list[GuardrailFlag]]:
    """``(ok, flags)`` for one tool result. Never raises.

    ``ok`` is False when any flag's action is ``blocked`` or ``sanitized``: the
    caller may still forward the payload (it has been inspected, not rewritten)
    but must not treat it as clean.
    """
    try:
        if not env.enabled():
            return True, []
        text, flags = _as_text(payload)
        cap = env.tool_max_bytes() if max_bytes is None else int(max_bytes)

        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            size = len(text.encode("utf-8", "replace"))
            flags.append(
                _flag("tool_output.encoding", "sanitized",
                      f"text is not valid UTF-8 ({exc.reason})")
            )
        if size > cap:
            flags.append(
                _flag("tool_output.size_cap", "sanitized",
                      f"{tool_name or 'tool'} returned {size} bytes (cap {cap})")
            )
        if "\ufffd" in text and text:
            ratio = text.count("\ufffd") / max(1, len(text))
            if ratio >= _REPLACEMENT_FLOOD:
                flags.append(
                    _flag("tool_output.encoding", "sanitized",
                          "payload is dominated by replacement characters")
                )
            else:
                flags.append(
                    _flag("tool_output.encoding", "warned",
                          "payload contains replacement characters")
                )
        control = _CONTROL.findall(text)
        if control:
            flags.append(
                _flag("tool_output.control_chars", "sanitized",
                      f"{len(control)} control character(s) in tool output")
            )

        flags.extend(_url_flags(text))

        hits = scan_injection(text)
        if hits:
            rules = sorted({hit.rule for hit in hits})
            flags.append(
                _flag(
                    "tool_output.embedded_instruction",
                    "sanitized",
                    f"{tool_name or 'tool'} output contains instruction-like text: "
                    + ", ".join(rules[:5]),
                )
            )
        ok = not any(flag.action in {"blocked", "sanitized"} for flag in flags)
        return ok, flags
    except Exception as exc:  # noqa: BLE001 - validation must never raise
        return False, [
            _flag("tool_output.validator_error", "sanitized",
                  f"validation failed: {type(exc).__name__}")
        ]
