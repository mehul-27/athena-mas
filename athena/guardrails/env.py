"""Environment switches for the guardrails layer.

Every switch is read at call time (never at import), so a test that sets an
environment variable and calls a guardrail sees the change, and a deployment can
tighten or loosen policy without a code edit.

| variable                              | default | meaning                                   |
|---------------------------------------|---------|-------------------------------------------|
| ``ATHENA_GUARDRAILS_ENABLED``         | ``1``   | master switch; ``0`` makes screening a no-op |
| ``ATHENA_GUARDRAILS_BLOCK``           | ``1``   | allow any flag to be ``blocked`` at all    |
| ``ATHENA_GUARDRAILS_BLOCK_TOXIC``     | ``0``   | also block high-severity toxicity          |
| ``ATHENA_GUARDRAILS_MAX_INPUT_CHARS`` | ``8000``| input text cap used by the chat screen     |
| ``ATHENA_GUARDRAILS_TOOL_MAX_BYTES``  | ``65536``| byte cap for one tool output              |
| ``ATHENA_GUARDRAILS_TOOL_ALLOW_LOCAL``| ``0``   | allow tool output to reference private hosts |
"""

from __future__ import annotations

import os

TRUTHY = frozenset({"1", "true", "yes", "on", "y"})
FALSY = frozenset({"0", "false", "no", "off", "n"})


def flag(name: str, default: bool) -> bool:
    """Read a boolean switch. Unset or unrecognised values fall back to default."""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in TRUTHY:
        return True
    if raw in FALSY:
        return False
    return default


def number(name: str, default: int, low: int = 1, high: int = 10_000_000) -> int:
    """Read an integer switch, clamped so a bad value cannot disable a check."""
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(low, min(high, value))


def enabled() -> bool:
    """Is the guardrails layer active?"""
    return flag("ATHENA_GUARDRAILS_ENABLED", True)


def blocking_enabled() -> bool:
    """May a guardrail decision refuse a request outright?"""
    return flag("ATHENA_GUARDRAILS_BLOCK", True)


def toxic_blocking() -> bool:
    """Treat high-severity toxicity as blocking (off by default: see docs)."""
    return flag("ATHENA_GUARDRAILS_BLOCK_TOXIC", False)


def max_input_chars() -> int:
    return number("ATHENA_GUARDRAILS_MAX_INPUT_CHARS", 8000, low=100)


def tool_max_bytes() -> int:
    return number("ATHENA_GUARDRAILS_TOOL_MAX_BYTES", 65536, low=256)


def tool_allow_local_hosts() -> bool:
    return flag("ATHENA_GUARDRAILS_TOOL_ALLOW_LOCAL", False)
