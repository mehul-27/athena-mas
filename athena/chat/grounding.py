"""Explicit, opt-in grounding for chat.

Nothing here runs unless the user enabled a capability. Both capabilities may be
on; sources are numbered continuously and the model is told to cite them.

Every source block is rendered inside the same untrusted-content boundary the
research pipeline uses (:mod:`athena.agents.untrusted`), because a retrieved
passage or a web snippet is data: it may contain text that looks like an
instruction, and the boundary plus the accompanying rule is what tells the model
not to treat it as one. Source text is kept verbatim — it is evidence — but any
spoofed boundary marker inside it is removed and instruction-like spans are
recorded as guardrail flags for the turn.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Iterable

from athena.agents.untrusted import wrap_untrusted
from athena.chat.models import ChatTarget, Grounded
from athena.chat.prompts import refusal_message
from athena.guardrails import env as guard_env
from athena.guardrails.injection import scan_injection

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


def _cap_blocks(blocks: list[str], limit: int) -> tuple[list[str], int]:
    """Keep whole blocks until the budget is spent.

    Truncating the joined string would cut a closing boundary marker and leave
    untrusted text unterminated, which is worse than dropping the source.
    """
    kept: list[str] = []
    used = 0
    for block in blocks:
        size = len(block) + 2
        if kept and used + size > limit:
            continue
        if not kept and size > limit:
            block = block[: max(0, limit - 2)]
            size = len(block) + 2
        kept.append(block)
        used += size
    return kept, len(blocks) - len(kept)


async def collect(
    query: str,
    capabilities: Iterable[str],
    target: ChatTarget,
) -> Grounded:
    caps = tuple(capabilities)
    sources: list[dict] = []
    blocks: list[str] = []
    notes: list[str] = []
    events: list[dict] = []
    hits: list = []

    if "rag" in caps:
        from athena.tools.rag.search import rag_search

        started = time.perf_counter()
        result = await asyncio.to_thread(rag_search, query, RAG_TOP_K)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        if not result.ok:
            notes.append(f"Document search failed: {result.error or 'unknown error'}")
            events.append(
                {
                    "tool": "rag_search",
                    "label": "Document search",
                    "status": "error",
                    "summary": "Search failed",
                    "error": result.error or "unknown error",
                    "elapsed_ms": elapsed,
                }
            )
        else:
            floor = _min_score()
            found = [hit for hit in result.hits if hit.score >= floor]
            if not found:
                notes.append("No document passage cleared the relevance threshold.")
            events.append(
                {
                    "tool": "rag_search",
                    "label": "Document search",
                    "status": "ok",
                    "summary": f"{len(found)} passage(s) used",
                    "error": "",
                    "elapsed_ms": elapsed,
                }
            )
            for hit in found:
                index = len(sources) + 1
                text = hit.text[:MAX_DOC_CHARS]
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
                hits.extend(scan_injection(text)[:5])
                blocks.append(
                    wrap_untrusted(
                        f"[{index}] Document: {hit.document}{page} "
                        f"(score {hit.score:.2f})\n{text}"
                    )
                )

    if "web_search" in caps:
        from athena.tools.web.search import web_search

        started = time.perf_counter()
        result = await asyncio.to_thread(
            web_search,
            query,
            WEB_RESULTS,
            provider=target.search_provider,
            endpoint=target.search_endpoint,
        )
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        if not result.ok:
            notes.append(f"Web search failed: {result.error or 'unknown error'}")
            events.append(
                {
                    "tool": "web_search",
                    "label": "Web search",
                    "status": "error",
                    "summary": "Search failed",
                    "error": result.error or "unknown error",
                    "elapsed_ms": elapsed,
                }
            )
        elif not result.results:
            notes.append("No web results were returned.")
            events.append(
                {
                    "tool": "web_search",
                    "label": "Web search",
                    "status": "ok",
                    "summary": "0 results",
                    "error": "",
                    "elapsed_ms": elapsed,
                }
            )
        else:
            events.append(
                {
                    "tool": "web_search",
                    "label": "Web search",
                    "status": "ok",
                    "summary": f"{len(result.results)} result(s) used",
                    "error": "",
                    "elapsed_ms": elapsed,
                }
            )
            for hit in result.results:
                index = len(sources) + 1
                snippet = hit.snippet[:MAX_WEB_CHARS]
                sources.append(
                    {
                        "kind": "web",
                        "index": index,
                        "title": hit.title,
                        "url": hit.url,
                        "snippet": snippet,
                    }
                )
                hits.extend(scan_injection(f"{hit.title} {snippet}")[:5])
                blocks.append(
                    wrap_untrusted(f"[{index}] Web: {hit.title} — {hit.url}\n{snippet}")
                )

    kept, dropped = _cap_blocks(blocks, MAX_CONTEXT_CHARS)
    if dropped:
        notes.append(
            f"{dropped} source(s) were left out to keep the reference context within budget."
        )
    if hits and guard_env.enabled():
        notes.append(
            "Some reference text contained instruction-like phrasing; it was kept "
            "as data and is not followed."
        )
    context = "\n\n".join(kept)
    refusal = "" if sources else refusal_message(caps, notes)
    return Grounded(
        context=context,
        sources=sources,
        notes=notes,
        refusal=refusal,
        events=events,
        injection_hits=list(hits),
    )

