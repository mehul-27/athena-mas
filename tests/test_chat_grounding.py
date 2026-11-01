"""Grounding selection, source numbering and honest refusals."""

from __future__ import annotations

import asyncio

from athena.chat import grounding
from athena.chat.models import ChatTarget
from athena.tools.schemas import RagHit, RagResult, WebHit, WebResult


def _target() -> ChatTarget:
    return ChatTarget(
        model="openai/fake",
        api_key="key",
        api_base="",
        reasoning_effort="",
        provider="openai",
        search_provider="duckduckgo",
    )


def _rag(*_args, **_kwargs) -> RagResult:
    return RagResult(
        query="q",
        top_k=2,
        elapsed_ms=1.0,
        hits=[
            RagHit(
                chunk_id="c1", document_id="d", document="handbook.pdf", chunk_index=0,
                page=3, text="systems engineering", score=0.8,
            ),
            RagHit(
                chunk_id="c2", document_id="d", document="handbook.pdf", chunk_index=1,
                page=4, text="life cycle", score=0.1,
            ),
        ],
    )


def _web(*_args, **_kwargs) -> WebResult:
    return WebResult(
        query="q",
        provider="duckduckgo",
        max_results=2,
        elapsed_ms=1.0,
        results=[WebHit(title="Python docs", url="https://docs.python.org/3.13/", snippet="release")],
    )


def test_both_capabilities_are_numbered_in_one_context(monkeypatch) -> None:
    monkeypatch.setattr("athena.tools.rag.search.rag_search", _rag)
    monkeypatch.setattr("athena.tools.web.search.web_search", _web)

    result = asyncio.run(grounding.collect("q", ["rag", "web_search"], _target()))

    assert [source["kind"] for source in result.sources] == ["document", "web"]
    assert [source["index"] for source in result.sources] == [1, 2]
    assert "[1]" in result.context and "[2]" in result.context
    assert result.refusal == ""


def test_the_rag_score_floor_drops_weak_hits(monkeypatch) -> None:
    monkeypatch.setattr("athena.tools.rag.search.rag_search", _rag)

    result = asyncio.run(grounding.collect("q", ["rag"], _target()))

    assert len(result.sources) == 1
    assert result.sources[0]["score"] == 0.8


def test_no_sources_produces_a_refusal(monkeypatch) -> None:
    monkeypatch.setattr(
        "athena.tools.web.search.web_search",
        lambda *_a, **_k: WebResult(
            query="q", provider="duckduckgo", max_results=1, elapsed_ms=1.0,
            results=[], note="no results",
        ),
    )

    result = asyncio.run(grounding.collect("q", ["web_search"], _target()))

    assert result.sources == []
    assert "couldn't find useful web results" in result.refusal
