"""Build the corpus index: extract, chunk, embed, store.

Kept apart from the server so scripts and tests can ingest without starting an
MCP process. Re-running is safe: chunk ids are content-addressed, so an
unchanged document adds nothing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from athena.tools.rag.corpus import document_chunks, text_chunks
from athena.tools.rag.index import RagIndex


def ingest_text(index: RagIndex, filename: str, text: str) -> dict:
    """Ingest a plain-text document (fixtures, notes, non-PDF corpora)."""
    document_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    chunks = text_chunks(text)
    stats = index.add_chunks(document_id, filename, chunks)
    return {
        "document": filename,
        "document_id": document_id,
        "chunks": len(chunks),
        "added": stats["added"],
        "skipped": stats["skipped"],
    }


def ingest_pdf(index: RagIndex, path: str | Path) -> dict:
    """Ingest one PDF and report what happened."""
    path = Path(path)
    document_id = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    chunks = document_chunks(path)
    stats = index.add_chunks(document_id, path.name, chunks)
    return {
        "document": path.name,
        "document_id": document_id,
        "chunks": len(chunks),
        "added": stats["added"],
        "skipped": stats["skipped"],
    }


def ingest_corpus(index: RagIndex, directory: str | Path) -> dict:
    """Ingest every ``*.pdf`` in a directory and return totals."""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"corpus directory not found: {directory}")

    files = [ingest_pdf(index, pdf) for pdf in sorted(directory.glob("*.pdf"))]
    return {
        "documents": len(files),
        "chunks": sum(entry["chunks"] for entry in files),
        "added": sum(entry["added"] for entry in files),
        "skipped": sum(entry["skipped"] for entry in files),
        "embedding_model": index.embeddings.model,
        "dimension": index.embeddings.dimension(),
        "indexed_chunks": index.count(),
        "files": files,
    }
