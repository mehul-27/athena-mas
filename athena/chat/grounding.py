"""Explicit, opt-in grounding for chat.

Nothing here runs unless the user enabled a capability. Both capabilities may be
on; sources are numbered continuously and the model is told to cite them.
"""

from __future__ import annotations

import asyncio
import os
from typing import Iterable

from athena.chat.models import ChatTarget, Grounded
from athena.chat.prompts import refusal_message

MAX_CONTEXT_CHARS = 12000
MAX_DOC_CHARS = 1200
MAX_WEB_CHARS = 600
RAG_TOP_K = 5
WEB_RESULTS = 5


def _min_score() -> float:
    try:
        return float(os.getenv("CHAT_RAG_MIN_SCORE") or 0.25)
    except ValueError:
        return 0.25


async def collect(
    query: str,
    capabilities: Iterable[str],
    target: ChatTarget,
) -> Grounded:
    caps = tuple(capabilities)
    sources: list[dict] = []
    blocks: list[str] = []
    notes: list[str] = []

    if "rag" in caps:
        from athena.tools.rag.search import rag_search

        result = await asyncio.to_thread(rag_search, query, RAG_TOP_K)
        if not result.ok:
            notes.append(f"Document search failed: {result.error or 'unknown error'}")
        else:
            floor = _min_score()
            hits = [hit for hit in result.hits if hit.score >= floor]
            if not hits:
                notes.append("No document passage cleared the relevance threshold.")
            for hit in hits:
                index = len(sources) + 1
                page = f", page {hit.page}" if hit.page else ""
                sources.append(
                    {
                        "kind": "document",
                        "index": index,
                        "document": hit.document,
                        "page": hit.page,
                        "chunk_id": hit.chunk_id,
                        "score": hit.score,
                        "snippet": hit.text[:400],
                    }
                )
                blocks.append(
                    f"[{index}] Document: {hit.document}{page} (score {hit.score:.2f})\n"
                    f"{hit.text[:MAX_DOC_CHARS]}"
                )

    if "web_search" in caps:
        from athena.tools.web.search import web_search

        result = await asyncio.to_thread(
            web_search,
            query,
            WEB_RESULTS,
            provider=target.search_provider,
            endpoint=target.search_endpoint,
        )
        if not result.ok:
            notes.append(f"Web search failed: {result.error or 'unknown error'}")
        elif not result.results:
            notes.append("No web results were returned.")
        else:
            for hit in result.results:
                index = len(sources) + 1
                sources.append(
                    {
                        "kind": "web",
                        "index": index,
                        "title": hit.title,
                        "url": hit.url,
                        "snippet": hit.snippet[:MAX_WEB_CHARS],
                    }
                )
                blocks.append(
                    f"[{index}] Web: {hit.title} — {hit.url}\n"
                    f"{hit.snippet[:MAX_WEB_CHARS]}"
                )

    context = "\n\n".join(blocks)[:MAX_CONTEXT_CHARS]
    refusal = "" if sources else refusal_message(caps, notes)
    return Grounded(context=context, sources=sources, notes=notes, refusal=refusal)
