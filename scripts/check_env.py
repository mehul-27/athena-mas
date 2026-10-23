"""Report which configuration variables are set. Values are never printed.

    python scripts/check_env.py

Secrets are reported as "SET (length N)"; nothing here reads .env or echoes a
value, so it is safe to paste the output into a report.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Loads .env the same way the app does; prints nothing.
import athena.config.llm  # noqa: F401,E402

SECRET_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")

VARIABLES = (
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_ITPM_LIMIT",
    "LLM_RATELIMIT_MAX_WAIT_S",
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


def main() -> int:
    missing = 0
    for name in VARIABLES:
        value = os.getenv(name)
        if not value:
            print(f"{name:<26} not set")
            continue
        if any(marker in name for marker in SECRET_MARKERS):
            print(f"{name:<26} SET (length {len(value)})")
        else:
            print(f"{name:<26} SET")

    if not os.getenv("LLM_API_KEY"):
        missing += 1
    if not os.getenv("LLM_MODEL"):
        missing += 1
    print()
    print("required missing:", missing)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
