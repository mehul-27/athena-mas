"""evidence_recorder: the deterministic after-tool hook for the researcher.

The model chooses which tool to call and with what arguments. This hook turns
whatever the tool actually returned into blackboard evidence, so the model never
has to copy a result and cannot misreport one.

It also hands the model a **compact** acknowledgement instead of the raw result.
FastMCP returns each result twice (text content plus ``structuredContent``), so
echoing it back would burn the provider's input-token budget for text the model
does not need — the evidence is already on the blackboard.

Rule for failures: a search or calculation that failed records **no** evidence
and returns the response untouched. A failed page fetch is recorded as
provenance (``source_type="other"``) so the attempt is visible, but it is not a
citable web source. A tool error is not evidence.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from athena.agents.untrusted import (
    UNTRUSTED_BEGIN,
    UNTRUSTED_DATA_RULE,
    wrap_untrusted,
)
from athena.blackboard import Blackboard, BlackboardStore
from athena.tools.adapters import rag_source_ref, to_evidence_items
from athena.tools.schemas import (
    CalcResult,
    FetchResult,
    ForecastResult,
    RagResult,
    ToolResult,
    WebResult,
)

AUTHOR = "researcher"
MAX_FETCH_INSPECT_CHARS = 4000

TOOL_MODELS: dict[str, type[ToolResult]] = {
    "rag_search": RagResult,
    "web_search": WebResult,
    "fetch_page": FetchResult,
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
    if isinstance(result, FetchResult):
        return {
            "untrusted_external_content": True,
            "content_boundary": UNTRUSTED_BEGIN,
            "status": result.status,
            "requested_url": result.requested_url,
            "final_url": result.final_url,
            "canonical_url": result.canonical_url,
            "title": result.title,
            "truncated": result.truncated,
            "inspect_truncated": len(result.text) > MAX_FETCH_INSPECT_CHARS,
            "text": wrap_untrusted(result.text[:MAX_FETCH_INSPECT_CHARS]),
            "note": (
                f"{UNTRUSTED_DATA_RULE} Full bounded page text is on the "
                "blackboard; cite this page only when its text supports the claim."
            ),
            "error": result.error,
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
        written = record_tool_result(
            run_id, tool_name, tool_response, store, sink, author=author
        )
        if not result.ok and not isinstance(result, FetchResult):
            # A failed search/calculation is not a source, but the model gets a
            # small answer instead of the whole payload it already could not use.
            return {"recorded": 0, "error": result.error or "tool failed"}
        payload = compact_result(tool_name, result)
        if isinstance(result, FetchResult):
            payload["recorded"] = written
        return payload

    return evidence_recorder