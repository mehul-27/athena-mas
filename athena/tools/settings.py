"""Environment-driven settings for Athena's MCP tools.

Every path and provider comes from an environment variable with a default, so
the tools behave the same from a checkout, from tests, and from an MCP
subprocess started with a different working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def data_dir() -> Path:
    return Path(os.getenv("ATHENA_DATA_DIR") or REPO_ROOT / "data")


def chroma_path() -> Path:
    return Path(os.getenv("CHROMA_PATH") or data_dir() / "chroma")


def corpus_dir() -> Path:
    return Path(os.getenv("CORPUS_DIR") or data_dir() / "corpus")


def eval_dir() -> Path:
    return Path(os.getenv("EVAL_DIR") or data_dir() / "eval")


def collection_name() -> str:
    return os.getenv("RAG_COLLECTION", "athena_documents")


def embedding_provider() -> str:
    return (os.getenv("EMBEDDING_PROVIDER") or "fastembed").lower()


def fastembed_model() -> str:
    return os.getenv("FASTEMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")


def fastembed_cache_path() -> Path:
    return Path(os.getenv("FASTEMBED_CACHE_PATH") or data_dir() / "fastembed_cache")


def chunk_size() -> int:
    return int(os.getenv("RAG_CHUNK_SIZE", "1000"))


def chunk_overlap() -> int:
    return int(os.getenv("RAG_CHUNK_OVERLAP", "200"))


def search_provider() -> str:
    return (os.getenv("SEARCH_PROVIDER") or "duckduckgo").lower()


def searxng_instance() -> str:
    return (os.getenv("SEARXNG_INSTANCE") or "").rstrip("/")


def user_agent() -> str:
    return os.getenv("WEB_FETCH_USER_AGENT") or DEFAULT_USER_AGENT


def search_timeout() -> float:
    return float(os.getenv("SEARCH_TIMEOUT", "15"))


def fetch_timeout() -> float:
    return float(os.getenv("WEB_FETCH_TIMEOUT", "12"))


def fetch_total_timeout() -> float:
    return float(os.getenv("WEB_FETCH_TOTAL_TIMEOUT", "20"))


def fetch_max_bytes() -> int:
    return int(os.getenv("WEB_FETCH_MAX_BYTES", "1000000"))


def fetch_max_chars() -> int:
    return int(os.getenv("WEB_FETCH_MAX_CHARS", "8000"))
