"""Report which configuration variables are set, and where each came from.

    python scripts/check_env.py

Never prints a secret. For keys it prints only the length; for a base URL only
the host (a URL can carry a path or a query string). "source shell" means the
variable was already in the environment before the app loaded .env.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

# Snapshot before importing the app, which loads .env.
_SHELL_VARS = set(os.environ)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import athena.config.llm  # noqa: E402,F401  (loads .env; prints nothing)

SECRET_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")

VARIABLES = (
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_API_BASE",
    "LLM_REASONING_EFFORT",
    "LLM_ITPM_LIMIT",
    "LLM_OTPM_LIMIT",
    "LLM_MAX_TOKENS",
    "SEARCH_PROVIDER",
    "SEARXNG_INSTANCE",
    "BRAVE_API_KEY",
    "TAVILY_API_KEY",
    "BLACKBOARD_DB",
    "CHROMA_PATH",
    "CORPUS_DIR",
    "EMBEDDING_PROVIDER",
    "ATHENA_STAGE_PAUSE",
    "ATHENA_MAX_REVISIONS",
    "ATHENA_MAX_TOOL_CALLS",
)


def _source(name: str) -> str:
    if not os.getenv(name):
        return "not set"
    return "shell" if name in _SHELL_VARS else ".env"


def _describe(name: str) -> str:
    value = os.getenv(name) or ""
    source = _source(name)
    if source == "not set":
        return "not set"
    if any(marker in name for marker in SECRET_MARKERS):
        return f"SET (length {len(value)}, source {source})"
    if name == "LLM_API_BASE":
        # Host only: the path and query are not ours to print.
        return f"SET (host {urlparse(value).netloc or '(none)'}, source {source})"
    if name in {"LLM_MODEL", "LLM_REASONING_EFFORT", "SEARCH_PROVIDER", "EMBEDDING_PROVIDER"}:
        return f"SET ({value}, source {source})"
    return f"SET (source {source})"


def main() -> int:
    for name in VARIABLES:
        print(f"{name:<26} {_describe(name)}")

    if os.getenv("LLM_MODEL"):
        from athena.config.llm import provider_key_var, provider_prefix

        prefix = provider_prefix()
        exported = provider_key_var() or "(none - LLM_API_KEY only)"
        print()
        print(f"{'provider prefix':<26} {prefix}")
        print(f"{'exported key variable':<26} {exported}")

    missing = (0 if os.getenv("LLM_API_KEY") else 1) + (0 if os.getenv("LLM_MODEL") else 1)
    print()
    print("required missing:", missing)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
