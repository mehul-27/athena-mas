"""Build the corpus index: extract, chunk, embed, store.

Kept apart from the server so scripts and tests can ingest without starting an
MCP process. Re-running is safe: chunk ids are content-addressed, so an
unchanged document adds nothing.

Every document is bounded before extraction, because ingest is the one place in
the application that reads a file the operator did not write:

| variable                     | default    | effect                          |
|------------------------------|------------|---------------------------------|
| ``ATHENA_RAG_MAX_FILE_BYTES``| ``52428800``| largest PDF (50 MiB)           |
| ``ATHENA_RAG_MAX_PAGES``     | ``2000``   | largest page count             |
| ``ATHENA_RAG_MAX_TEXT_CHARS``| ``2000000``| largest plain-text document    |

A document over a limit is refused, not truncated: half a source silently
indexed is worse than a recorded refusal. The refusal is reported in the stats
dict (``refused``), so a corpus run keeps going and the caller can print why.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from athena.tools.rag.corpus import document_chunks, text_chunks
from athena.tools.rag.index import RagIndex

DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_PAGES = 2000
DEFAULT_MAX_TEXT_CHARS = 2_000_000


def _number(name: str, default: int, low: int = 1) -> int:
    try:
        return max(low, int(os.getenv(name) or default))
    except ValueError:
        return default


def max_file_bytes() -> int:
    return _number("ATHENA_RAG_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES, low=1024)


def max_pages() -> int:
    return _number("ATHENA_RAG_MAX_PAGES", DEFAULT_MAX_PAGES)


def max_text_chars() -> int:
    return _number("ATHENA_RAG_MAX_TEXT_CHARS", DEFAULT_MAX_TEXT_CHARS, low=1000)


def _refused(document: str, reason: str) -> dict:
    return {
        "document": document,
        "document_id": "",
        "chunks": 0,
        "added": 0,
        "skipped": 0,
        "refused": reason,
    }


def pdf_page_count(path: str | Path) -> int | None:
    """Page count without extracting text, or None when the file is unreadable."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:  # noqa: BLE001 - an unreadable PDF is refused below
        return None


def ingest_text(index: RagIndex, filename: str, text: str) -> dict:
    """Ingest a plain-text document (fixtures, notes, non-PDF corpora)."""
    cap = max_text_chars()
    if len(text) > cap:
        return _refused(filename, f"text is {len(text)} chars (cap {cap})")
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
    size = path.stat().st_size
    cap = max_file_bytes()
    if size > cap:
        return _refused(path.name, f"file is {size} bytes (cap {cap})")
    pages = pdf_page_count(path)
    page_cap = max_pages()
    if pages is not None and pages > page_cap:
        return _refused(path.name, f"document has {pages} pages (cap {page_cap})")
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
        "refused": sum(1 for entry in files if entry.get("refused")),
        "embedding_model": index.embeddings.model,
        "dimension": index.embeddings.dimension(),
        "indexed_chunks": index.count(),
        "files": files,
    }
