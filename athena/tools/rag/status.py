"""Read-only RAG index status without loading an embedding model.

`RagIndex.open()` builds an embedding provider (fastembed loads an ONNX model),
which is far too heavy for a status endpoint. The collection count only needs
Chroma's local SQLite-backed client, so that is what this module opens.
"""

from __future__ import annotations

import logging
from pathlib import Path

from athena.tools import settings

logger = logging.getLogger(__name__)


def chunk_count(path: str | Path | None = None, collection: str | None = None) -> int:
    """Number of indexed chunks, or 0 when the index is missing/unreadable."""
    target = Path(path or settings.chroma_path())
    if not target.exists():
        return 0
    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(target))
        return int(client.get_collection(collection or settings.collection_name()).count())
    except Exception as exc:  # noqa: BLE001 - a status probe must not raise
        logger.info("RAG index status unavailable: %s", type(exc).__name__)
        return 0


def index_exists(path: str | Path | None = None) -> bool:
    return (Path(path or settings.chroma_path()) / "chroma.sqlite3").exists()


def document_rows(
    path: str | Path | None = None,
    collection: str | None = None,
    batch: int = 1000,
) -> list[dict]:
    """Distinct indexed documents with chunk/page counts.

    Read-only and lightweight enough for a Documents view: only Chroma metadata
    is read, so no embedding model is loaded.
    """
    target = Path(path or settings.chroma_path())
    if not target.exists():
        return []
    found: dict[str, dict] = {}
    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(target))
        store = client.get_collection(collection or settings.collection_name())
        offset = 0
        while True:
            raw = store.get(include=["metadatas"], limit=batch, offset=offset)
            ids = raw.get("ids") or []
            if not ids:
                break
            metadatas = raw.get("metadatas") or []
            for index, chunk_id in enumerate(ids):
                meta = metadatas[index] if index < len(metadatas) else {}
                if not isinstance(meta, dict):
                    meta = {}
                key = str(meta.get("document_id") or meta.get("filename") or chunk_id)
                row = found.setdefault(
                    key,
                    {
                        "id": key,
                        "name": str(meta.get("filename") or key),
                        "chunks": 0,
                        "pages": set(),
                    },
                )
                row["chunks"] += 1
                if meta.get("page"):
                    row["pages"].add(int(meta["page"]))
            offset += len(ids)
            if len(ids) < batch:
                break
    except Exception as exc:  # noqa: BLE001 - an inventory must not raise
        logger.info("RAG document inventory unavailable: %s", type(exc).__name__)
        return []
    rows = []
    for row in found.values():
        pages = sorted(row.pop("pages"))
        row["page_count"] = len(pages)
        row["first_page"] = pages[0] if pages else None
        rows.append(row)
    return sorted(rows, key=lambda item: item["name"].lower())


def document_preview(
    doc_id: str,
    path: str | Path | None = None,
    collection: str | None = None,
    max_chunks: int = 12,
    max_chars: int = 12000,
    batch: int = 500,
    max_scanned: int = 20000,
) -> dict | None:
    """Capped, read-only preview of one indexed document's chunk text.

    ``doc_id`` is the key :func:`document_rows` reports (``document_id``, else
    ``filename``, else the chunk id). Only stored chunk text is returned: no
    embedding model is loaded and nothing is written. The result is capped by
    both chunk count and characters and says so via ``truncated``. Returns
    ``None`` when the index is absent or the document is not in it, so a caller
    can answer 404 instead of an empty preview.
    """
    wanted = str(doc_id or "").strip()
    if not wanted:
        return None
    target = Path(path or settings.chroma_path())
    if not target.exists():
        return None
    limit_chunks = max(1, int(max_chunks))
    limit_chars = max(1, int(max_chars))
    chunks: list[dict] = []
    pages: set[int] = set()
    total = 0
    used = 0
    truncated = False
    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(target))
        store = client.get_collection(collection or settings.collection_name())
        offset = 0
        scanned = 0
        while scanned < max_scanned:
            raw = store.get(include=["metadatas", "documents"], limit=batch, offset=offset)
            ids = raw.get("ids") or []
            if not ids:
                break
            metadatas = raw.get("metadatas") or []
            documents = raw.get("documents") or []
            for index, chunk_id in enumerate(ids):
                meta = metadatas[index] if index < len(metadatas) else {}
                if not isinstance(meta, dict):
                    meta = {}
                key = str(meta.get("document_id") or meta.get("filename") or chunk_id)
                if key != wanted:
                    continue
                total += 1
                page = meta.get("page")
                if page:
                    try:
                        pages.add(int(page))
                    except (TypeError, ValueError):
                        pass
                if len(chunks) >= limit_chunks:
                    truncated = True
                    continue
                body = documents[index] if index < len(documents) else ""
                body = str(body or "")
                room = limit_chars - used
                if room <= 0:
                    truncated = True
                    continue
                if len(body) > room:
                    body = body[:room]
                    truncated = True
                used += len(body)
                chunks.append({"index": total, "page": page, "text": body})
            scanned += len(ids)
            offset += len(ids)
            if len(ids) < batch:
                break
    except Exception as exc:  # noqa: BLE001 - a preview must not raise
        logger.info("RAG document preview unavailable: %s", type(exc).__name__)
        return None
    if not total:
        return None
    return {
        "id": wanted,
        "name": wanted,
        "chunk_count": total,
        "pages": sorted(pages),
        "chunks": chunks,
        "truncated": truncated,
    }
