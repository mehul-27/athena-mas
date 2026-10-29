"""MCP server exposing ``web_search`` over stdio.

Run: ``python -m athena.tools.web_server``
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # also allow `python athena/tools/web_server.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server.fastmcp import FastMCP

from athena.tools.schemas import FetchResult, WebResult
from athena.tools.web.fetch import fetch_page as _fetch_page
from athena.tools.web.search import web_search as _web_search

mcp = FastMCP("athena-web")


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> WebResult:
    """Search the web and return titles, URLs and snippets.

    Args:
        query: The search query.
        max_results: Maximum number of results to return (1-20).
    """
    return _web_search(query, max_results)


@mcp.tool()
def fetch_page(url: str, max_chars: int = 6000) -> FetchResult:
    """Read a public web page so claims can cite page text, not just snippets.

    Args:
        url: Absolute public http/https URL.
        max_chars: Maximum readable characters to return (1000-20000).
    """
    return _fetch_page(url, max_chars)


if __name__ == "__main__":
    mcp.run(transport="stdio")
