"""MCP server exposing ``web_search`` over stdio.

Run: ``python -m athena.tools.web_server``

Schema notes (verified by ``tests/test_tools_adapters.py``):

- ``max_results`` and ``max_chars`` are range-bounded in the published JSON
  schema, so an out-of-range request is rejected as a protocol-level validation
  error instead of being silently clamped. A caller that asks for 200 results
  must not receive 20 while believing it received 200.
- No tool raises: ``web_search``/``fetch_page`` return ``ok=False`` with an error
  message, so a provider failure is data the agent can reason about rather than a
  transport error that kills the turn.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

if __package__ in (None, ""):  # also allow `python athena/tools/web_server.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from athena.tools.schemas import FetchResult, WebResult
from athena.tools.web.fetch import fetch_page as _fetch_page
from athena.tools.web.search import web_search as _web_search

mcp = FastMCP("athena-web")

MAX_RESULTS_MIN, MAX_RESULTS_MAX = 1, 20
MAX_CHARS_MIN, MAX_CHARS_MAX = 1000, 20000


@mcp.tool()
def web_search(
    query: str,
    max_results: Annotated[
        int, Field(ge=MAX_RESULTS_MIN, le=MAX_RESULTS_MAX)
    ] = 5,
) -> WebResult:
    """Search the web and return titles, URLs and snippets.

    Args:
        query: The search query.
        max_results: Maximum number of results to return (1-20).
    """
    return _web_search(query, max_results)


@mcp.tool()
def fetch_page(
    url: str,
    max_chars: Annotated[int, Field(ge=MAX_CHARS_MIN, le=MAX_CHARS_MAX)] = 6000,
) -> FetchResult:
    """Read a public web page so claims can cite page text, not just snippets.

    Args:
        url: Absolute public http/https URL.
        max_chars: Maximum readable characters to return (1000-20000).
    """
    return _fetch_page(url, max_chars)


if __name__ == "__main__":
    mcp.run(transport="stdio")
