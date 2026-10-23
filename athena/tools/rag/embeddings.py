"""Embedding providers for the RAG index.

Ported from the old Athena `rag/embeddings.py`: `fastembed` (a local ONNX model,
no service) is the default, and `hashing` is a deterministic offline
bag-of-words embedding so tests and the no-download path are reproducible. The
HTTP provider is dropped — Athena no longer configures an embedding service.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

from athena.tools import settings
from athena.tools.rag.text import tokenize

# The repo can live on a drive where HuggingFace's default symlinked cache fails
# (WinError 1463), so the cache must be copy-based. These must be set before
# `huggingface_hub` is first imported, hence module level.
if os.name == "nt":
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

HASHING_DIM = 256


class EmbeddingProvider(Protocol):
    """What the index needs from an embedding backend."""

    model: str

    def dimension(self) -> int: ...

    def encode(self, texts: Sequence[str]) -> np.ndarray: ...


def _normalize(vectors: np.ndarray) -> np.ndarray:
    if vectors.size == 0:
        return vectors
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0, 1.0, norms)


class FastEmbedProvider:
    """Local ONNX embeddings via `fastembed`. No service, works offline."""

    def __init__(self, model: str | None = None, cache_dir: str | Path | None = None) -> None:
        from fastembed import TextEmbedding

        self.model = model or settings.fastembed_model()
        cache = Path(cache_dir or settings.fastembed_cache_path())
        cache.mkdir(parents=True, exist_ok=True)
        self._embedding = TextEmbedding(model_name=self.model, cache_dir=str(cache))
        self._dim: int | None = None

    def dimension(self) -> int:
        if self._dim is None:
            self._dim = int(self.encode(["hello"]).shape[1])
        return self._dim

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimension()), dtype="float32")
        vectors = np.array(list(self._embedding.embed(list(texts))), dtype="float32")
        return _normalize(vectors)


class HashingEmbeddingProvider:
    """Deterministic offline feature hashing over content words.

    Two texts sharing no content word are orthogonal, which makes relevance
    assertions deterministic — that is the whole point of this provider.
    """

    def __init__(self, dim: int = HASHING_DIM) -> None:
        self.model = f"hashing-{dim}"
        self._dim = dim

    def dimension(self) -> int:
        return self._dim

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype="float32")
        out = np.zeros((len(texts), self._dim), dtype="float32")
        for row, text in enumerate(texts):
            for token in tokenize(text):
                digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
                out[row, int(digest, 16) % self._dim] += 1.0
        return _normalize(out)


def build_embedding_provider(provider: str | None = None) -> EmbeddingProvider:
    """Build the provider named by EMBEDDING_PROVIDER (default: fastembed)."""
    name = (provider or settings.embedding_provider()).lower()
    if name == "hashing":
        return HashingEmbeddingProvider()
    if name == "fastembed":
        return FastEmbedProvider()
    raise ValueError(f"Unknown EMBEDDING_PROVIDER: {name!r} (use fastembed or hashing)")
