"""Per-pattern LLM call ceilings.

A single global cap is wrong: the parallel pattern legitimately needs several
times the calls of a sequential run, so a value tuned for parallel lets a
sequential run loop far too long, and one tuned for sequential kills parallel.
ATHENA_MAX_LLM_CALLS still overrides every pattern.
"""

from __future__ import annotations

import os

CAP_ENV = "ATHENA_MAX_LLM_CALLS"

PATTERN_CAPS = {
    "sequential": 12,
    "p2p": 12,
    "parallel": 30,
    "hierarchical": 40,
}
DEFAULT_CAP = 12


def llm_call_cap(pattern: str) -> int:
    """The cap for this pattern; the env var wins when it is set."""
    override = (os.getenv(CAP_ENV) or "").strip()
    if override.isdigit():
        return int(override)
    return PATTERN_CAPS.get(pattern, DEFAULT_CAP)


def cap_message(pattern: str, cap: int) -> str:
    return (
        f"Run stopped: LLM call cap {cap} reached for pattern {pattern}. "
        f"Raise {CAP_ENV}."
    )
