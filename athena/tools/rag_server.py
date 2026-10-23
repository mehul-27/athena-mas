"""MCP server exposing ``rag_search`` over stdio.

Run: ``python -m athena.tools.rag_server``
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # also allow `python athena/tools/rag_server.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server.fastmcp import FastMCP

from athena.tools.rag.search import rag_search as _rag_search
from athena.tools.schemas import RagResult

mcp = FastMCP("athena-rag")


@mcp.tool()
def rag_search(query: str, top_k: int = 5) -> RagResult:
    """Search Athena's NASA document corpus and return the closest passages.

    Args:
        query: A natural-language question or keywords.
        top_k: Maximum number of passages to return (1-20).
    """
    return _rag_search(query, top_k)


if __name__ == "__main__":
    mcp.run(transport="stdio")
