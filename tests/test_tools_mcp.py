"""MCP integration: spawn each tool server over stdio and call a tool.

The tests run the real subprocess and read the real ``structuredContent``, so
they prove the wire format, not just the Python functions.

Why ``e2e``: spawning a server needs anonymous pipes for its stdin/stdout.
This sandbox denies creating them (``PermissionError: [WinError 5]`` inside
``asyncio.windows_utils``), so the default offline run cannot exercise stdio at
all. The tests are kept intact and run with::

    pytest tests/test_tools_mcp.py -m e2e
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from athena.tools.rag.embeddings import HashingEmbeddingProvider
from athena.tools.rag.index import RagIndex
from athena.tools.rag.ingest import ingest_text
from athena.tools.schemas import CalcResult, RagResult, WebResult

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS = {
    "orion.txt": "The Orion spacecraft carries crew beyond low Earth orbit.",
    "propulsion.txt": "Ion propulsion accelerates propellant electrically over years.",
}


async def _call(module: str, calls: list[tuple[str, dict, type]], env: dict[str, str]):
    """Start one server, list its tools, run each call, validate the payload."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", module],
        cwd=str(REPO_ROOT),
        env={**os.environ, **env},
    )
    results = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            names = [tool.name for tool in listing.tools]
            for name, arguments, model in calls:
                raw = await session.call_tool(name, arguments)
                assert raw.structuredContent is not None, f"{name} had no structured output"
                results.append(model.model_validate(raw.structuredContent))
    return names, results


def test_calc_server_over_stdio() -> None:
    names, (result,) = asyncio.run(
        _call(
            "athena.tools.calc_server",
            [("calculate", {"expression": "17.5 + 24.5"}, CalcResult)],
            env={},
        )
    )
    assert set(names) == {"calculate", "forecast"}
    assert result.ok, result.error
    assert result.value == 42.0


def test_rag_server_over_stdio(tmp_path) -> None:
    chroma = tmp_path / "chroma"
    built = RagIndex.open(
        embeddings=HashingEmbeddingProvider(), path=chroma, name="stdio_fixture"
    )
    for name, text in CORPUS.items():
        ingest_text(built, name, text)

    names, (result,) = asyncio.run(
        _call(
            "athena.tools.rag_server",
            [("rag_search", {"query": "How does ion propulsion work?", "top_k": 2}, RagResult)],
            env={
                "EMBEDDING_PROVIDER": "hashing",
                "CHROMA_PATH": str(chroma),
                "RAG_COLLECTION": "stdio_fixture",
            },
        )
    )
    assert names == ["rag_search"]
    assert result.ok, result.error
    assert result.hits
    assert result.hits[0].document == "propulsion.txt"


def test_web_server_over_stdio_reports_disabled() -> None:
    names, (result,) = asyncio.run(
        _call(
            "athena.tools.web_server",
            [("web_search", {"query": "nasa", "max_results": 2}, WebResult)],
            env={"SEARCH_PROVIDER": "disabled"},
        )
    )
    assert names == ["web_search"]
    assert not result.ok
    assert "disabled" in (result.error or "").lower()


@pytest.mark.e2e
def test_web_server_over_stdio_real_search() -> None:
    _, (result,) = asyncio.run(
        _call(
            "athena.tools.web_server",
            [("web_search", {"query": "NASA systems engineering", "max_results": 2}, WebResult)],
            env={"SEARCH_PROVIDER": "duckduckgo"},
        )
    )
    assert result.ok, result.error
    if not result.results:
        # DuckDuckGo answers an anti-bot page with 202 and no result blocks;
        # that is the provider refusing us, not the tool misbehaving.
        pytest.skip("search provider returned no results (anti-bot page)")
    assert result.results[0].url.startswith("http")
