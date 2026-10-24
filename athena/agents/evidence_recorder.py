"""evidence_recorder: the deterministic after-tool hook for the researcher.

The model chooses which tool to call and with what arguments. This hook turns
whatever the tool actually returned into blackboard evidence, so the model never
has to copy a result and cannot misreport one.

It also hands the model a **compact** acknowledgement instead of the raw result.
FastMCP returns each result twice (text content plus ``structuredContent``), so
echoing it back would burn the provider's input-token budget for text the model
does not need — the evidence is already on the blackboard.

Rule for failures: a tool call that failed (``ok=false``, or an MCP-level
``isError``) records **no** evidence and returns the response untouched. A tool
error is not a source.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import ValidationError

from athena.blackboard import Blackboard, BlackboardStore
from athena.tools.adapters import rag_source_ref, to_evidence_items
from athena.tools.schemas import (
    CalcResult,
    ForecastResult,
    RagResult,
    ToolResult,
    WebResult,
)

AUTHOR = "researcher"

# A hard ceiling on tool calls per researcher run. Every tool round-trip is
# re-sent on the next model call, so a chatty researcher exhausts a free tier's
# input-token budget and the run fails. Past the budget the hook stops
# recording and tells the model to finish.
MAX_TOOL_CALLS = int(os.getenv("ATHENA_MAX_TOOL_CALLS", "3"))

TOOL_MODELS: dict[str, type[ToolResult]] = {
    "rag_search": RagResult,
    "web_search": WebResult,
    "calculate": CalcResult,
    "forecast": ForecastResult,
}


def _payload(tool_response: Any) -> dict[str, Any] | None:
    """Pull the model payload out of ADK's tool-response wrapper."""
    if isinstance(tool_response, str):
        try:
            tool_response = json.loads(tool_response)
        except ValueError:
            return None
    if not isinstance(tool_response, dict):
        return None
    if tool_response.get("isError"):
        return None
    structured = tool_response.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    return tool_response


def parse_result(tool_name: str, tool_response: Any) -> ToolResult | None:
    """The typed tool result, or None when it cannot be trusted."""
    model = TOOL_MODELS.get(tool_name)
    payload = _payload(tool_response)
    if model is None or payload is None:
        return None
    try:
        return model.model_validate(payload)
    except ValidationError:
        return None


def compact_result(tool_name: str, result: ToolResult) -> dict[str, Any]:
    """The small summary the model sees instead of the full payload."""
    if isinstance(result, RagResult):
        top = result.hits[0] if result.hits else None
        return {
            "recorded": len(result.hits),
            "top_source": rag_source_ref(top) if top else None,
            "top_score": top.score if top else None,
            "note": "Chunks are saved to the blackboard; do not quote them back.",
        }
    if isinstance(result, WebResult):
        return {
            "recorded": len(result.results),
            "sources": [hit.url for hit in result.results],
            "note": "Results are saved to the blackboard.",
        }
    if isinstance(result, CalcResult):
        return {"recorded": 1, "value": result.value, "expression": result.expression}
    if isinstance(result, ForecastResult):
        return {"recorded": 1, "forecast": result.forecast, "method": result.method}
    return {"recorded": 0, "tool": tool_name}


def record_tool_result(
    run_id: str,
    tool_name: str,
    tool_response: Any,
    store: BlackboardStore | None = None,
    sink: Any = None,
    author: str = AUTHOR,
) -> int:
    """Turn one tool result into evidence. Returns how many items landed.

    ``sink`` is the p2p path: the items are handed to the caller instead of
    being written to the blackboard, because p2p agents never touch it.
    """
    result = parse_result(tool_name, tool_response)
    if result is None:
        return 0
    items = to_evidence_items(result, retrieved_by=author)
    if not items:
        return 0

    if sink is not None:
        sink(items)
        return len(items)

    blackboard = Blackboard.open(run_id, store)
    # Idempotent: a retried stage must not duplicate evidence it already wrote.
    existing = blackboard.read("evidence")
    known = {item.id for item in existing[0]} if existing else set()

    written = 0
    for item in items:
        if item.id in known:
            continue
        blackboard.append("evidence", item, author=author)
        known.add(item.id)
        written += 1
    return written


def make_evidence_recorder(
    run_id: str, store: BlackboardStore | None = None, sink: Any = None
):
    """Build the callback ADK calls after every researcher tool call."""
    def evidence_recorder(tool, args, tool_context, tool_response):
        tool_name = getattr(tool, "name", "")
        # The worker that made the call owns the evidence it produced.
        author = getattr(tool_context, "agent_name", "") or AUTHOR
        result = parse_result(tool_name, tool_response)
        if result is None:
            # Nothing trustworthy arrived: let the model see the original error.
            return None
        record_tool_result(run_id, tool_name, tool_response, store, sink, author=author)
        if not result.ok:
            # A failed tool is not evidence, but the model gets a small answer
            # instead of the whole payload it already could not use.
            return {"recorded": 0, "error": result.error or "tool failed"}
        return compact_result(tool_name, result)

    return evidence_recorder

class ToolBudget:
    """Shared counter so a worker cannot loop on tool calls forever.

    Every tool round-trip is re-sent on the next model call, so an unbounded
    loop burns the provider quota and the run never ends.
    """

    def __init__(self, limit: int = MAX_TOOL_CALLS) -> None:
        self.limit = limit
        self.calls = 0

    def take(self) -> bool:
        self.calls += 1
        return self.calls <= self.limit


def make_tool_guard(budget: ToolBudget):
    """Refuse the tool call once the budget is gone (ADK skips the call)."""

    def before_tool(tool, args, tool_context):
        if budget.take():
            return None
        return {
            "recorded": 0,
            "note": f"Tool budget of {budget.limit} calls reached. Reply DONE now.",
        }

    return before_tool