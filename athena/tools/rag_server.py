"""MCP server exposing ``rag_search`` over stdio.

Run: ``python -m athena.tools.rag_server``

``top_k`` is range-bounded in the published schema (see ``web_server`` for why a
silent clamp is worse than a validation error). The tool itself never raises: an
unusable index, an empty query or a missing embedding model comes back as
``ok=False`` so the agent can report the limitation instead of crashing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

if __package__ in (None, ""):  # also allow `python athena/tools/rag_server.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from athena.tools.rag.search import rag_search as _rag_search
from athena.tools.schemas import RagResult

mcp = FastMCP("athena-rag")

TOP_K_MIN, TOP_K_MAX = 1, 20


@mcp.tool()
def rag_search(
    query: str,
    top_k: Annotated[int, Field(ge=TOP_K_MIN, le=TOP_K_MAX)] = 5,
) -> RagResult:
    """Search Athena's NASA document corpus and return the closest passages.

    Args:
        query: A natural-language question or keywords.
        top_k: Maximum number of passages to return (1-20).
    """
    return _rag_search(query, top_k)


if __name__ == "__main__":
    mcp.run(transport="stdio")
