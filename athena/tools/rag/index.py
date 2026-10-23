"""Chroma-backed vector index for the corpus.

Ported from the old Athena `rag/chroma.py` + `rag/engine.py`: an embedded
``PersistentClient`` (no service to run), content-addressed chunk ids so
re-indexing is idempotent, and an embedding fingerprint stored on the
collection so a changed model or dimension is visible instead of silently
returning nonsense.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Sequence

from athena.tools import settings
from athena.tools.rag.embeddings import EmbeddingProvider, build_embedding_provider

logger = logging.getLogger(__name__)

ID_PREFIX = "doc_"


def chunk_id(document_id: str, text: str) -> str:
    """Stable id for a chunk, scoped by document so identical text is distinct."""
    digest = hashlib.sha256(f"{document_id}\x00{text}".encode("utf-8")).hexdigest()[:16]
    return f"{ID_PREFIX}{digest}"


def _fingerprint(model: str, dimension: int) -> str:
    return hashlib.sha256(f"{model}\n{dimension}".encode("utf-8")).hexdigest()[:16]


class RagIndex:
    """A Chroma collection and the embeddings that built it."""

    def __init__(self, collection: Any, embeddings: EmbeddingProvider) -> None:
        self.collection = collection
        self.embeddings = embeddings

    @classmethod
    def open(
        cls,
        embeddings: EmbeddingProvider | None = None,
        path: str | Path | None = None,
        name: str | None = None,
    ) -> "RagIndex":
        """Open (or create) the index named by CHROMA_PATH / RAG_COLLECTION."""
        import chromadb

        embeddings = embeddings or build_embedding_provider()
        dimension = embeddings.dimension()
        target = Path(path or settings.chroma_path())
        target.mkdir(parents=True, exist_ok=True)

        metadata = {
            "hnsw:space": "cosine",
            "embedding_model": embeddings.model,
            "embedding_dimension": dimension,
            "embedding_fingerprint": _fingerprint(embeddings.model, dimension),
        }
        collection_name = name or settings.collection_name()
        client = chromadb.PersistentClient(path=str(target))
        try:
            collection = client.get_collection(collection_name)
        except Exception:
            collection = client.get_or_create_collection(
                name=collection_name, metadata=metadata
            )

        existing = (collection.metadata or {}).get("embedding_fingerprint")
        if existing and existing != metadata["embedding_fingerprint"]:
            logger.warning(
                "Collection %s was built with a different embedding model; "
                "re-run ingestion before trusting retrieval.",
                collection_name,
            )
        return cls(collection, embeddings)

    def count(self) -> int:
        try:
            return int(self.collection.count())
        except Exception:
            return 0

    def add_chunks(
        self,
        document_id: str,
        filename: str,
        chunks: Sequence[tuple[str, int | None]],
    ) -> dict[str, int]:
        """Embed and index chunks, skipping ids that already exist."""
        if not chunks:
            return {"added": 0, "skipped": 0}

        ids = [chunk_id(document_id, text) for text, _ in chunks]
        metadatas = []
        for index, (_, page) in enumerate(chunks):
            meta: dict[str, Any] = {
                "document_id": document_id,
                "filename": filename,
                "chunk_index": index,
            }
            if page:
                meta["page"] = int(page)
            metadatas.append(meta)

        try:
            existing = set(self.collection.get(ids=ids).get("ids") or [])
        except Exception:
            existing = set()

        keep = [i for i, cid in enumerate(ids) if cid not in existing]
        if not keep:
            return {"added": 0, "skipped": len(ids)}

        texts = [chunks[i][0] for i in keep]
        self.collection.add(
            ids=[ids[i] for i in keep],
            embeddings=self.embeddings.encode(texts).tolist(),
            documents=texts,
            metadatas=[metadatas[i] for i in keep],
        )
        return {"added": len(keep), "skipped": len(ids) - len(keep)}

    def query(self, text: str, k: int) -> list[dict[str, Any]]:
        """Raw nearest-neighbour rows: id, text, metadata, distance."""
        total = self.count()
        if total == 0 or not text.strip():
            return []

        raw = self.collection.query(
            query_embeddings=self.embeddings.encode([text]).tolist(),
            n_results=max(1, min(k, total)),
            include=["documents", "metadatas", "distances"],
        )
        ids = (raw.get("ids") or [[]])[0]
        documents = (raw.get("documents") or [[]])[0]
        metadatas = (raw.get("metadatas") or [[]])[0]
        distances = (raw.get("distances") or [[]])[0]

        rows = []
        for i, chunk in enumerate(ids):
            rows.append(
                {
                    "id": chunk,
                    "text": documents[i] if i < len(documents) else "",
                    "metadata": (metadatas[i] if i < len(metadatas) else {}) or {},
                    "distance": float(distances[i]) if i < len(distances) else 1.0,
                }
            )
        return rows
