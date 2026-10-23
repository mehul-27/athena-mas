"""Offline RAG test: tiny three-document fixture, hashing embeddings, temp Chroma."""

from __future__ import annotations

import pytest

from athena.tools.rag.embeddings import HashingEmbeddingProvider
from athena.tools.rag.index import RagIndex
from athena.tools.rag.ingest import ingest_text
from athena.tools.rag.search import get_index, rag_search, search_index
from athena.tools.schemas import RagResult

DOCUMENTS = {
    "orion.txt": "The Orion spacecraft carries four crew members beyond low Earth orbit. "
    "Its European service module supplies propulsion, power and life support.",
    "solar-array.txt": "The space station solar arrays convert sunlight into electricity. "
    "Each array generates about thirty kilowatts of direct current power.",
    "propulsion.txt": "Ion propulsion accelerates propellant electrically. "
    "The engine produces very low thrust but can run for years, which makes it "
    "useful for deep space missions.",
}


@pytest.fixture()
def rag_env(monkeypatch, tmp_path):
    """Point the index at a throwaway Chroma directory and the offline embedder."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hashing")
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("RAG_COLLECTION", "fixture_corpus")
    get_index.cache_clear()
    yield
    get_index.cache_clear()


@pytest.fixture()
def index(rag_env):
    built = RagIndex.open(embeddings=HashingEmbeddingProvider())
    for name, text in DOCUMENTS.items():
        ingest_text(built, name, text)
    return built


def test_query_returns_expected_document(index: RagIndex) -> None:
    hits = search_index(index, "How does ion propulsion work?", top_k=3)
    assert hits
    assert hits[0].document == "propulsion.txt"
    assert hits[0].score > 0
    assert hits[0].text


def test_rag_search_tool_returns_typed_result(index: RagIndex) -> None:
    result = rag_search("How does ion propulsion work?", top_k=2)
    assert result.ok, result.error
    assert result.tool_name == "rag_search"
    assert len(result.hits) <= 2
    assert result.hits[0].document == "propulsion.txt"
    # The payload must survive a round trip through the model.
    RagResult.model_validate(result.model_dump())


def test_rag_search_rejects_empty_query(index: RagIndex) -> None:
    result = rag_search("   ")
    assert not result.ok
    assert "query" in (result.error or "").lower()


def test_ingest_is_idempotent(index: RagIndex) -> None:
    before = index.count()
    ingest_text(index, "propulsion.txt", DOCUMENTS["propulsion.txt"])
    assert index.count() == before
