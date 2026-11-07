"""rag_search: hybrid retrieval over the corpus, returned as a typed result.

Ported from the old Athena `rag/retrieval.py`: 0.7 * vector similarity +
0.3 * keyword overlap over the same candidate set. The score is a relative
hybrid score, not a calibrated probability.
"""

from __future__ import annotations

import time
from functools import lru_cache

from athena.tools import settings
from athena.tools.rag.index import RagIndex
from athena.tools.rag.text import token_set
from athena.tools.schemas import RagHit, RagResult

VECTOR_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3


@lru_cache(maxsize=1)
def get_index() -> RagIndex:
    """The index described by the current environment. Cached per process."""
    return RagIndex.open()


def clear_index_cache() -> None:
    """Forget the cached index so a rebuild is visible without a restart."""
    get_index.cache_clear()


def search_index(index: RagIndex, query: str, top_k: int = 5) -> list[RagHit]:
    """Hybrid search over an already-open index. Raises on an unusable index."""
    query = " ".join((query or "").split())
    if not query:
        raise ValueError("query must not be empty")

    candidates = index.query(query, max(top_k * 3, top_k + 5))
    query_tokens = token_set(query)

    def hit_from_row(row, *, adjacent: bool = False) -> RagHit:
        vector_similarity = max(0.0, min(1.0, 1.0 - row["distance"]))
        document_tokens = token_set(row["text"])
        overlap = len(query_tokens & document_tokens)
        keyword_score = overlap / len(query_tokens) if query_tokens else 0.0
        score = (
            KEYWORD_WEIGHT * keyword_score
            if adjacent
            else VECTOR_WEIGHT * vector_similarity + KEYWORD_WEIGHT * keyword_score
        )
        meta = row["metadata"]
        return RagHit(
            chunk_id=row["id"],
            document_id=str(meta.get("document_id", "")),
            document=str(meta.get("filename", "")),
            chunk_index=int(meta.get("chunk_index", -1)),
            page=int(meta["page"]) if meta.get("page") else None,
            text=row["text"],
            score=round(score, 4),
        )

    hits = [hit_from_row(row) for row in candidates]
    hits.sort(key=lambda hit: hit.score, reverse=True)
    top = hits[:top_k]

    # Expand the best contiguous passages only when the caller asked for a
    # retrieval set large enough to preserve a list/table. This is not a change
    # to ingestion or chunk size; it is a targeted context expansion.
    radius = settings.rag_adjacent_chunks()
    if radius and top_k >= 3:
        seen = {hit.chunk_id for hit in top}
        for hit in top[:3]:
            for row in index.adjacent(hit.document_id, hit.chunk_index, radius):
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                top.append(hit_from_row(row, adjacent=True))
    return top


def rag_search(query: str, top_k: int = 5) -> RagResult:
    """Search the corpus. Never raises: failures come back as ``ok=False``."""
    started = time.perf_counter()
    limit = max(1, min(int(top_k), 20))
    try:
        hits = search_index(get_index(), query, limit)
        return RagResult(
            query=query,
            top_k=limit,
            hits=hits,
            elapsed_ms=_elapsed(started),
        )
    except Exception as exc:  # noqa: BLE001 - a tool must report, not raise
        return RagResult(
            query=query,
            top_k=limit,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=_elapsed(started),
        )


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
