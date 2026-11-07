"""Canonical registry of the tools a research run can expose.

The browser sends tool ids, and every layer maps those ids through this module.
That keeps the API, the planner prompt, the MCP server filters and the worker
dispatch in agreement: a name that is not here can never be enabled, and an
enabled name is the only thing a worker is allowed to receive.

``None`` in a selection means "all tools"; an empty tuple means "no tools".
Both are real states and must stay distinguishable all the way to the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from athena.tools import settings


@dataclass(frozen=True)
class ResearchTool:
    id: str
    label: str
    description: str
    role: str
    server: str


RESEARCH_TOOLS: tuple[ResearchTool, ...] = (
    ResearchTool(
        "rag_search",
        "Local RAG",
        "Search the indexed local document corpus.",
        "rag_researcher",
        "rag",
    ),
    ResearchTool(
        "web_search",
        "Web search",
        "Search the web through the configured provider.",
        "web_researcher",
        "web",
    ),
    ResearchTool(
        "fetch_page",
        "Fetch page",
        "Read the contents of one public web page.",
        "web_researcher",
        "web",
    ),
    ResearchTool(
        "calculate",
        "Calculation",
        "Evaluate an arithmetic expression safely.",
        "calc_researcher",
        "calc",
    ),
    ResearchTool(
        "forecast",
        "Forecasting",
        "Project a numeric series with a linear trend or smoothing.",
        "calc_researcher",
        "calc",
    ),
)

TOOL_BY_ID = {tool.id: tool for tool in RESEARCH_TOOLS}
ALL_TOOL_IDS = tuple(tool.id for tool in RESEARCH_TOOLS)
ROLE_ORDER = ("rag_researcher", "web_researcher", "calc_researcher")
ROLE_TOOL_IDS = {
    role: tuple(tool.id for tool in RESEARCH_TOOLS if tool.role == role)
    for role in ROLE_ORDER
}
ROLE_LABELS = {
    "rag_researcher": "RAG researcher",
    "web_researcher": "Web researcher",
    "calc_researcher": "Calculation researcher",
}


class UnknownToolError(ValueError):
    """A selected tool id is not in the registry."""


def normalise_selection(selected: Iterable[str] | None) -> tuple[str, ...] | None:
    """Return a canonical selection, or ``None`` for All tools.

    Ordering follows the registry, duplicates are dropped, and unknown ids are
    rejected here rather than being passed to an agent and failing later.
    """
    if selected is None:
        return None
    if isinstance(selected, str):
        selected = [selected]
    clean: list[str] = []
    unknown: list[str] = []
    for raw in selected:
        name = str(raw or "").strip()
        if not name or name in clean:
            continue
        if name not in TOOL_BY_ID:
            unknown.append(name)
            continue
        clean.append(name)
    if unknown:
        raise UnknownToolError(
            f"unknown research tool(s): {', '.join(sorted(unknown))}; "
            f"expected one of {list(ALL_TOOL_IDS)}"
        )
    return tuple(tool for tool in ALL_TOOL_IDS if tool in clean)


def enabled_ids(selected: Iterable[str] | None) -> tuple[str, ...]:
    """The concrete enabled ids; All tools expands to the whole registry."""
    resolved = normalise_selection(selected)
    return ALL_TOOL_IDS if resolved is None else resolved


def is_enabled(tool_id: str, selected: Iterable[str] | None) -> bool:
    return tool_id in enabled_ids(selected)


def task_selection(run_id: str, store=None) -> tuple[str, ...] | None:
    """The selection recorded on a run's task, or All tools when absent."""
    from athena.blackboard import Blackboard

    task = Blackboard.open(run_id, store).read("task")
    if not task:
        return None
    return normalise_selection(task[0].tools)


def role_tools(role: str, selected: Iterable[str] | None) -> tuple[str, ...]:
    """The enabled tool ids owned by one worker role."""
    allowed = set(enabled_ids(selected))
    return tuple(tool for tool in ROLE_TOOL_IDS.get(role, ()) if tool in allowed)


def role_enabled(role: str, selected: Iterable[str] | None) -> bool:
    return bool(role_tools(role, selected))


def available_roles(selected: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(role for role in ROLE_ORDER if role_enabled(role, selected))


def tools_for_roles(roles: Iterable[str], selected: Iterable[str] | None) -> tuple[str, ...]:
    """Enabled tools that the given plan roles are allowed to use."""
    allowed = set(enabled_ids(selected))
    wanted = set(roles)
    return tuple(
        tool.id
        for tool in RESEARCH_TOOLS
        if tool.role in wanted and tool.id in allowed
    )


def server_filters(selected: Iterable[str] | None) -> dict[str, list[str]]:
    """MCP server name -> tool names it may expose for this selection."""
    allowed = set(enabled_ids(selected))
    filters: dict[str, list[str]] = {}
    for tool in RESEARCH_TOOLS:
        if tool.id in allowed:
            filters.setdefault(tool.server, []).append(tool.id)
    return filters


def missing_roles(selected: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(role for role in ROLE_ORDER if not role_enabled(role, selected))


def describe_selection(selected: Iterable[str] | None) -> str:
    enabled = enabled_ids(selected)
    if not enabled:
        return "No research tools are enabled for this run."
    labels = [TOOL_BY_ID[name].label for name in enabled]
    return ", ".join(labels)


def capability_block(selected: Iterable[str] | None) -> str:
    """Prompt text listing what the planner may and may not plan."""
    enabled = enabled_ids(selected)
    disabled = [tool.id for tool in RESEARCH_TOOLS if tool.id not in enabled]
    lines = [
        "ENABLED RESEARCH TOOLS: " + (", ".join(enabled) or "(none)"),
    ]
    if disabled:
        lines.append(
            "DISABLED RESEARCH TOOLS: " + ", ".join(disabled)
            + ". Do not plan work that requires a disabled tool."
        )
        lines.append(
            "If the question requires a disabled capability, say so explicitly in "
            "the plan text instead of pretending it can be performed."
        )
    return "\n".join(lines)


def limit_note(selected: Iterable[str] | None) -> str:
    enabled = enabled_ids(selected)
    if not enabled:
        return (
            "No research tools are enabled, so no evidence can be gathered. "
            "State that limitation instead of answering from prior knowledge."
        )
    return (
        "Use only the enabled tools. A tool being enabled is not evidence that it "
        "ran; base claims on tool results actually recorded."
    )


def tool_ids_from_event_summary(summary: str | None) -> str | None:
    """Extract a registry tool name from an event's ``<tool>: <args>`` summary."""
    text = str(summary or "").strip()
    if not text:
        return None
    name = text.split(":", 1)[0].strip()
    return name if name in TOOL_BY_ID else None


def catalog(search_provider: str | None = None) -> list[dict]:
    """What the selector shows: registry metadata and current availability."""
    from athena.tools.rag.status import chunk_count

    provider = (search_provider or settings.search_provider() or "").lower()
    rows: list[dict] = []
    for tool in RESEARCH_TOOLS:
        available = True
        reason = ""
        if tool.id == "rag_search":
            available = chunk_count() > 0
            if not available:
                reason = "No documents are indexed. Rebuild the index in Settings → Library."
        elif tool.id == "web_search":
            available = provider in {"duckduckgo", "searxng"}
            if not available:
                reason = (
                    "Web search is disabled."
                    if provider == "disabled"
                    else f"Unsupported search provider {provider!r}."
                )
        rows.append(
            {
                "id": tool.id,
                "label": tool.label,
                "description": tool.description,
                "role": tool.role,
                "server": tool.server,
                "available": available,
                "reason": reason,
            }
        )
    return rows
