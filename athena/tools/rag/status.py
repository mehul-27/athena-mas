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
