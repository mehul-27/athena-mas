"""Convert tool results into blackboard ``EvidenceItem`` objects.

Each tool's output maps onto the blackboard's evidence model without changing
the blackboard. A failed result (``ok=False``) produces no evidence: a tool
error is not a source.
"""

from __future__ import annotations

import hashlib

from athena.blackboard.schemas import EvidenceItem
from athena.tools.schemas import (
    CalcResult,
    ForecastResult,
    RagResult,
    ToolResult,
    WebResult,
)


def _evidence_id(prefix: str, key: str) -> str:
    """Deterministic id, so re-running the same tool call adds no duplicates."""
    return f"{prefix}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"


def to_evidence_items(result: ToolResult, retrieved_by: str) -> list[EvidenceItem]:
    """Map one tool result onto evidence items attributed to ``retrieved_by``."""
    if not result.ok:
        return []
    if isinstance(result, RagResult):
        return _from_rag(result, retrieved_by)
    if isinstance(result, WebResult):
        return _from_web(result, retrieved_by)
    if isinstance(result, CalcResult):
        return _from_calc(result, retrieved_by)
    if isinstance(result, ForecastResult):
        return _from_forecast(result, retrieved_by)
    raise TypeError(f"cannot convert {type(result).__name__} to EvidenceItem")


def _from_rag(result: RagResult, retrieved_by: str) -> list[EvidenceItem]:
    items = []
    for hit in result.hits:
        ref = f"{hit.document}#chunk-{hit.chunk_index}"
        if hit.page:
            ref = f"{hit.document}#p{hit.page}-chunk-{hit.chunk_index}"
        items.append(
            EvidenceItem(
                id=hit.chunk_id,
                source_type="rag",
                source_ref=ref,
                content=hit.text,
                score=hit.score,
                retrieved_by=retrieved_by,
            )
        )
    return items


def _from_web(result: WebResult, retrieved_by: str) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            id=_evidence_id("web", hit.url),
            source_type="web",
            source_ref=hit.url,
            content=f"{hit.title}\n{hit.snippet}".strip(),
            score=None,
            retrieved_by=retrieved_by,
        )
        for hit in result.results
    ]


def _from_calc(result: CalcResult, retrieved_by: str) -> list[EvidenceItem]:
    content = f"{result.expression} = {result.value:.12g}"
    return [
        EvidenceItem(
            id=_evidence_id("calc", content),
            source_type="calculation",
            source_ref=f"calculate:{result.expression}",
            content=content,
            score=None,
            retrieved_by=retrieved_by,
        )
    ]


def _from_forecast(result: ForecastResult, retrieved_by: str) -> list[EvidenceItem]:
    values = ", ".join(f"{value:.6g}" for value in result.forecast)
    content = f"{result.method} forecast for {result.horizon} steps: {values}. {result.note}"
    return [
        EvidenceItem(
            id=_evidence_id("forecast", content),
            source_type="forecast",
            source_ref="forecast",
            content=content,
            score=None,
            retrieved_by=retrieved_by,
        )
    ]
