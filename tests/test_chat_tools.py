"""The chat tool catalogue, allowlist and bounded execution."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from athena.chat import tools
from athena.tools.schemas import RagHit, RagResult


def test_catalogue_is_the_real_mas_tool_set() -> None:
    assert set(tools.TOOL_SPECS) == {
        "rag_search",
        "web_search",
        "fetch_page",
        "calculate",
        "forecast",
    }
    rows = tools.catalog(SimpleNamespace(search_provider="duckduckgo", search_endpoint=""))
    assert {row["id"] for row in rows} == set(tools.TOOL_SPECS)
    assert all(row["server"].startswith("athena-") for row in rows)


def test_schemas_are_openai_function_shape() -> None:
    schemas = tools.schemas(["calculate"])
    assert schemas == [
        {
            "type": "function",
            "function": {
                "name": "calculate",
                "description": tools.TOOL_SPECS["calculate"].description,
                "parameters": tools.TOOL_SPECS["calculate"].parameters,
            },
        }
    ]


def test_unknown_tools_are_rejected_and_never_execute() -> None:
    selection = tools.resolve_selection(["nope", "calculate"], None)
    assert selection.allowed == ("calculate",)
    assert selection.rejected == ("nope",)
    assert selection.reason_map()["nope"] == "Unknown tool."


def test_web_search_availability_follows_the_provider() -> None:
    usable, reason = tools.availability(
        "web_search", SimpleNamespace(search_provider="disabled", search_endpoint="")
    )
    assert usable is False
    assert "disabled" in reason


def test_calculate_executes_and_reports_an_event() -> None:
    text, event = asyncio.run(tools.execute("calculate", {"expression": "2 + 3 * 4"}, None))

    assert "14" in text
    assert event["status"] == "ok"
    assert event["name"] == "calculate"


def test_arguments_are_clamped(monkeypatch) -> None:
    captured: dict = {}

    def fake_rag(query: str, top_k: int = 5) -> RagResult:
        captured.update({"query": query, "top_k": top_k})
        return RagResult(
            query=query,
            top_k=top_k,
            elapsed_ms=1.0,
            hits=[
                RagHit(
                    chunk_id="c1",
                    document_id="d1",
                    document="handbook.pdf",
                    chunk_index=0,
                    page=2,
                    text="evidence",
                    score=0.8,
                )
            ],
        )

    monkeypatch.setattr("athena.tools.rag.search.rag_search", fake_rag)

    text, event = asyncio.run(
        tools.execute("rag_search", {"query": "q", "top_k": 999}, None)
    )

    assert captured == {"query": "q", "top_k": 20}
    assert "handbook.pdf" in text
    assert event["status"] == "ok"


def test_tool_failure_is_a_result_not_an_exception(monkeypatch) -> None:
    def boom(query: str, top_k: int = 5):
        raise RuntimeError("nope")

    monkeypatch.setattr("athena.tools.rag.search.rag_search", boom)

    _, event = asyncio.run(tools.execute("rag_search", {"query": "q"}, None))

    assert event["status"] == "error"
    assert "RuntimeError" in event["error"]


def test_tool_timeout_is_reported(monkeypatch) -> None:
    def slow(*_args, **_kwargs):
        time.sleep(0.2)
        return "late", "late", True, ""

    monkeypatch.setattr(tools, "_run", slow)

    _, event = asyncio.run(tools.execute("calculate", {"expression": "1+1"}, None, timeout=0.01))

    assert event["status"] == "error"
    assert "timed out" in event["error"].lower()


def test_unknown_tool_execution_returns_an_event() -> None:
    text, event = asyncio.run(tools.execute("rm_rf", {}, None))
    assert "not available" in text
    assert event["status"] == "error"
