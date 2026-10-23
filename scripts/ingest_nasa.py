"""Build the RAG index from the NASA corpus.

Usage:
    python scripts/ingest_nasa.py                # EMBEDDING_PROVIDER from env
    python scripts/ingest_nasa.py hashing        # force the offline provider

Re-running is safe: chunk ids are content-addressed, so an unchanged corpus
adds nothing.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena.tools import settings
from athena.tools.rag.embeddings import build_embedding_provider
from athena.tools.rag.index import RagIndex
from athena.tools.rag.ingest import ingest_corpus


def main() -> int:
    requested = sys.argv[1] if len(sys.argv) > 1 else None
    corpus = settings.corpus_dir()
    if not corpus.is_dir() or not any(corpus.glob("*.pdf")):
        print(f"error: no PDFs found in {corpus}")
        print("Put the NASA handbook there, or point CORPUS_DIR at the folder.")
        return 1

    started = time.perf_counter()
    index = RagIndex.open(embeddings=build_embedding_provider(requested))
    stats = ingest_corpus(index, corpus)
    elapsed = time.perf_counter() - started

    provider = requested or settings.embedding_provider()
    print(f"corpus directory:   {corpus}")
    print(f"chroma path:        {settings.chroma_path()}")
    print(f"embedding provider: {provider} ({stats['embedding_model']}, dim {stats['dimension']})")
    print(f"documents:          {stats['documents']}")
    print(f"chunks produced:    {stats['chunks']}")
    print(f"chunks added:       {stats['added']} (already present: {stats['skipped']})")
    print(f"chunks in index:    {stats['indexed_chunks']}")
    for entry in stats["files"]:
        print(f"  - {entry['document']}: {entry['chunks']} chunks")
    print(f"elapsed:            {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
