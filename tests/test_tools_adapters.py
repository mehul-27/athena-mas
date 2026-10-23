"""Tool results convert into valid blackboard EvidenceItems."""

from __future__ import annotations

from athena.tools.adapters import to_evidence_items
from athena.tools.schemas import (
    CalcResult,
    ForecastResult,
    RagHit,
    RagResult,
    WebHit,
    WebResult,
)


def test_rag_result_becomes_rag_evidence() -> None:
    result = RagResult(
        query="q",
        top_k=2,
        elapsed_ms=2.0,
        hits=[
            RagHit(
                chunk_id="doc_abc",
                document_id="d1",
                document="handbook.pdf",
                chunk_index=7,
                page=12,
                text="A retrieved passage.",
                score=0.91,
            ),
            RagHit(
                chunk_id="doc_def",
                document_id="d1",
                document="handbook.pdf",
                chunk_index=8,
                text="A passage with no verified page.",
                score=0.42,
            ),
        ],
    )

    items = to_evidence_items(result, retrieved_by="researcher")

    assert len(items) == 2
    assert items[0].source_type == "rag"
    assert items[0].source_ref == "handbook.pdf#p12-chunk-7"
    assert items[0].content == "A retrieved passage."
    assert items[0].score == 0.91
    assert items[0].retrieved_by == "researcher"
    assert items[1].source_ref == "handbook.pdf#chunk-8"


def test_web_result_becomes_web_evidence() -> None:
    result = WebResult(
        query="q",
        provider="duckduckgo",
        max_results=1,
        elapsed_ms=1.0,
        results=[WebHit(title="NASA", url="https://nasa.gov", snippet="The agency.")],
    )

    items = to_evidence_items(result, retrieved_by="researcher")

    assert len(items) == 1
    assert items[0].source_type == "web"
    assert items[0].source_ref == "https://nasa.gov"
    assert "NASA" in items[0].content
    assert "The agency." in items[0].content
    assert items[0].score is None


def test_calc_result_becomes_calculation_evidence() -> None:
    result = CalcResult(expression="17.5 + 24.5", value=42.0, elapsed_ms=0.5)

    items = to_evidence_items(result, retrieved_by="analyst")

    assert len(items) == 1
    assert items[0].source_type == "calculation"
    assert items[0].source_ref == "calculate:17.5 + 24.5"
    assert "42" in items[0].content


def test_forecast_result_becomes_forecast_evidence() -> None:
    result = ForecastResult(
        values=[1, 2, 3, 4, 5],
        horizon=2,
        forecast=[6.0, 7.0],
        method="linear_trend",
        note="Straight line.",
        elapsed_ms=0.5,
    )

    items = to_evidence_items(result, retrieved_by="analyst")

    assert len(items) == 1
    assert items[0].source_type == "forecast"
    assert "6" in items[0].content


def test_failed_results_produce_no_evidence() -> None:
    failed = RagResult(query="q", top_k=3, elapsed_ms=0.1, ok=False, error="boom")
    assert to_evidence_items(failed, retrieved_by="researcher") == []


def test_unknown_result_type_is_rejected() -> None:
    class NotAResult:
        ok = True

    try:
        to_evidence_items(NotAResult(), retrieved_by="researcher")
    except TypeError as exc:
        assert "EvidenceItem" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("expected TypeError")
