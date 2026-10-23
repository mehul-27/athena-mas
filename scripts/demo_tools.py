"""Demo: drive the three MCP tool servers over stdio and print what they return.

Run after ``python scripts/ingest_nasa.py``.
"""

from __future__ import annotations

import asyncio
import csv
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from athena.tools import settings
from athena.tools.schemas import CalcResult, ForecastResult, RagResult, WebResult

SERVERS = {
    "rag": "athena.tools.rag_server",
    "web": "athena.tools.web_server",
    "calc": "athena.tools.calc_server",
}


async def run_server(label: str, calls: list[tuple[str, dict, type]]) -> list:
    """Open one stdio session, list its tools, run each call, validate results."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", SERVERS[label]],
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
    )
    results = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            print(f"[{label}] tools: {[tool.name for tool in listing.tools]}")
            for name, arguments, model in calls:
                raw = await session.call_tool(name, arguments)
                if raw.structuredContent is None:
                    raise RuntimeError(f"{name} returned no structured content")
                results.append(model.model_validate(raw.structuredContent))
    return results


def first_question() -> str:
    path = settings.eval_dir() / "nasa_rag_40_questions.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        return next(csv.DictReader(handle))["question"]


async def main() -> None:
    question = first_question()
    print(f"question (from data/eval): {question}\n")

    print("\n--- rag_search ---")
    (rag,) = await run_server(
        "rag", [("rag_search", {"query": question, "top_k": 3}, RagResult)]
    )
    print(f"ok={rag.ok} hits={len(rag.hits)} elapsed={rag.elapsed_ms}ms error={rag.error}")
    for hit in rag.hits:
        print(f"  score {hit.score:.4f}  {hit.document} p.{hit.page} chunk {hit.chunk_index}")
        print(f"    {hit.text[:110]!r}")

    print("\n--- calculate + forecast ---")
    calc, projection = await run_server(
        "calc",
        [
            ("calculate", {"expression": "17.5 + 24.5"}, CalcResult),
            ("forecast", {"values": [1, 2, 3, 4, 5], "horizon": 3}, ForecastResult),
        ],
    )
    print(f"calculate: {calc.expression} = {calc.value} (ok={calc.ok}, {calc.elapsed_ms}ms)")
    print(f"forecast:  {projection.forecast} via {projection.method} (ok={projection.ok})")
    print(f"  note: {projection.note}")

    print("\n--- web_search ---")
    (web,) = await run_server(
        "web",
        [("web_search", {"query": "NASA systems engineering handbook", "max_results": 3}, WebResult)],
    )
    print(f"provider={web.provider} ok={web.ok} results={len(web.results)} error={web.error}")
    for entry in web.results:
        print(f"  {entry.title}\n    {entry.url}")


if __name__ == "__main__":
    asyncio.run(main())
