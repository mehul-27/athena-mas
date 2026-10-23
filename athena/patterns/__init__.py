"""Pattern registry: sequential | parallel | hierarchical | p2p.

``build_supervisor`` in ``athena/agents/supervisor`` dispatches here. Each
builder returns a ready ADK agent for one blackboard run; none of them duplicate
agent code, they only choose which agents run and in what order.
"""

from __future__ import annotations

from typing import Any

VALID_PATTERNS = ("sequential", "parallel", "hierarchical", "p2p")
BLACKBOARD_PATTERNS = ("sequential", "parallel", "hierarchical")


class UnknownPatternError(ValueError):
    """Raised for a pattern name that is not in the registry."""


def build_pattern(name: str, run_id: str, store: Any = None, card_url: str = ""):
    """Build the supervisor for ``name``."""
    key = (name or "sequential").strip().lower()
    if key not in VALID_PATTERNS:
        raise UnknownPatternError(
            f"unknown pattern {name!r}; choose one of {list(VALID_PATTERNS)}"
        )

    from athena.patterns import hierarchical, parallel, p2p, sequential

    builders = {
        "sequential": sequential.build,
        "parallel": parallel.build,
        "hierarchical": hierarchical.build,
        "p2p": p2p.build,
    }
    return builders[key](run_id, store, card_url)
