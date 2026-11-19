"""evidence_recorder: the deterministic after-tool hook for the researcher.

The model chooses which tool to call and with what arguments. This hook turns
whatever the tool actually returned into blackboard evidence, so the model never
has to copy a result and cannot misreport one.

It also hands the model a **compact** acknowledgement instead of the raw result.
FastMCP returns each result twice (text content plus ``structuredContent``), so
echoing it back would burn the provider's input-token budget for text the model
does not need — the evidence is already on the blackboard.

Rule for failures: a search or calculation that failed records **no** evidence
and returns a bounded acknowledgement. A failed page fetch is recorded as
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
from athena.guardrails.secrets import redact_text
from athena.guardrails.tool_output import validate_tool_output
from athena.observability.guardrail_events import record_decision
from athena.tools.adapters import rag_source_ref, screen_items, to_evidence_items
from athena.tools.schemas import (
    CalcResult,
    FetchResult,
    ForecastResult,
    RagResult,
    ToolResult,
    WebResult,
)
from athena.tools.web.fetch import MAX_URL_LENGTH, FetchBlocked, normalize_url

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


def _fetch_preview_url(raw: str) -> str:
    """Keep only bounded HTTP(S) metadata suitable for the researcher."""
    try:
        url, _, _ = normalize_url(raw)
    except FetchBlocked:
        return ""
    if not raw.isprintable():
        return ""
    ok, _ = validate_tool_output("fetch_page", url, emit=False)
    if not ok:
        return ""
    redacted = redact_text(url) or ""
    return redacted if len(redacted) <= MAX_URL_LENGTH else ""


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
        preview = "\n".join(
            part for part in (result.title, result.text, result.error) if part
        )
        return {
            "untrusted_external_content": True,
            "content_boundary": UNTRUSTED_BEGIN,
            "status": result.status,
            "requested_url": _fetch_preview_url(result.requested_url),
            "final_url": _fetch_preview_url(result.final_url),
            "canonical_url": _fetch_preview_url(result.canonical_url),
            "truncated": result.truncated,
            "inspect_truncated": len(preview) > MAX_FETCH_INSPECT_CHARS,
            "text": wrap_untrusted((redact_text(preview) or "")[:MAX_FETCH_INSPECT_CHARS]),
            "note": (
                f"{UNTRUSTED_DATA_RULE} Full bounded page text is on the "
                "blackboard; cite this page only when its text supports the claim."
            ),
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
    dropped: list | None = None,
) -> int:
    """Turn one tool result into evidence. Returns how many items landed.

    ``sink`` is the p2p path: the items are handed to the caller instead of
    being written to the blackboard, because p2p agents never touch it.

    ``dropped`` is an optional out-parameter: guardrail flags for the items
    rejected here (over the byte cap, or not valid text), so the caller can show
    that a source existed and was refused rather than silently losing it.
    """
    result = parse_result(tool_name, tool_response)
    if result is None:
        return 0
    items, flags, _rejected = screen_items(
        to_evidence_items(result, retrieved_by=author), tool_name
    )
    if dropped is not None:
        dropped.extend(flags)
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
            # None tells ADK to pass the original response straight to the model.
            record_decision(
                stage="tool_output", rule="tool_output.invalid_result", action="sanitized",
                detail="tool returned an invalid result or a transport error",
                screen="make_evidence_recorder", pipeline_stage="tool_output", agent=author,
            )
            return {"recorded": 0, "error": "Tool returned no valid result."}
        dropped: list = []
        written = record_tool_result(
            run_id, tool_name, tool_response, store, sink, author=author,
            dropped=dropped,
        )
        if not result.ok and not isinstance(result, FetchResult):
            error = result.error or "Tool failed."
            error_ok, _ = validate_tool_output(tool_name, error, agent=author)
            return {
                "recorded": 0,
                "error": (redact_text(error) or "")[:500] if error_ok else "Tool failed.",
            }
        if isinstance(result, FetchResult) and dropped:
            payload = {"error": "Page result was withheld by guardrails."}
        else:
            payload = compact_result(tool_name, result)
        if isinstance(result, FetchResult):
            payload["recorded"] = written
        if dropped:
            # A source existed and the guardrails refused it: say so, so the
            # model does not report "no results" for a payload that was dropped.
            payload["dropped_by_guardrails"] = len(dropped)
            payload["guardrail_flags"] = [flag.model_dump() for flag in dropped]
        return payload

    return evidence_recorder
