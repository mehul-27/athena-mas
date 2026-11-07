"""Tool results convert into valid blackboard EvidenceItems.

The bottom half of this file checks the MCP surface itself: that every one of the
five research tools is really registered on a FastMCP server, that its published
JSON schema carries the declared types and bounds, that a call comes back as a
typed structured result (never an exception), and that provider diagnostics
cannot leak a secret.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from athena.tools.adapters import to_evidence_items
from athena.tools.schemas import (
    CalcResult,
    FetchResult,
    ForecastResult,
    RagHit,
    RagResult,
    WebHit,
    WebResult,
)


def test_rag_result_becomes_rag_evidence() -> None:
    result = RagResult(
        query="q",
        top_k=2,
        elapsed_ms=2.0,
        hits=[
            RagHit(
                chunk_id="doc_abc",
                document_id="d1",
                document="handbook.pdf",
                chunk_index=7,
                page=12,
                text="A retrieved passage.",
                score=0.91,
            ),
            RagHit(
                chunk_id="doc_def",
                document_id="d1",
                document="handbook.pdf",
                chunk_index=8,
                text="A passage with no verified page.",
                score=0.42,
            ),
        ],
    )

    items = to_evidence_items(result, retrieved_by="researcher")

    assert len(items) == 2
    assert items[0].source_type == "rag"
    assert items[0].source_ref == "handbook.pdf#p12-chunk-7"
    assert items[0].content == "A retrieved passage."
    assert items[0].score == 0.91
    assert items[0].retrieved_by == "researcher"
    assert items[1].source_ref == "handbook.pdf#chunk-8"


def test_web_result_becomes_web_evidence() -> None:
    result = WebResult(
        query="q",
        provider="duckduckgo",
        max_results=1,
        elapsed_ms=1.0,
        results=[WebHit(title="NASA", url="https://nasa.gov", snippet="The agency.")],
    )

    items = to_evidence_items(result, retrieved_by="researcher")

    assert len(items) == 1
    assert items[0].source_type == "web"
    assert items[0].source_ref == "https://nasa.gov"
    assert "NASA" in items[0].content
    assert "The agency." in items[0].content
    assert items[0].score is None
    assert items[0].provider == "duckduckgo"


def test_calc_result_becomes_calculation_evidence() -> None:
    result = CalcResult(expression="17.5 + 24.5", value=42.0, elapsed_ms=0.5)

    items = to_evidence_items(result, retrieved_by="analyst")

    assert len(items) == 1
    assert items[0].source_type == "calculation"
    assert items[0].source_ref == "calculate:17.5 + 24.5"
    assert "42" in items[0].content


def test_forecast_result_becomes_forecast_evidence() -> None:
    result = ForecastResult(
        values=[1, 2, 3, 4, 5],
        horizon=2,
        forecast=[6.0, 7.0],
        method="linear_trend",
        note="Straight line.",
        elapsed_ms=0.5,
    )

    items = to_evidence_items(result, retrieved_by="analyst")

    assert len(items) == 1
    assert items[0].source_type == "forecast"
    assert "6" in items[0].content


def test_failed_results_produce_no_evidence() -> None:
    failed = RagResult(query="q", top_k=3, elapsed_ms=0.1, ok=False, error="boom")
    assert to_evidence_items(failed, retrieved_by="researcher") == []


def test_a_failed_search_produces_no_evidence_at_all() -> None:
    """A provider failure is not a source, and must never invent one."""
    failed = WebResult(
        query="q",
        provider="duckduckgo",
        max_results=3,
        elapsed_ms=1.0,
        ok=False,
        error="search provider throttled",
    )
    assert to_evidence_items(failed, retrieved_by="researcher") == []


def test_a_failed_fetch_is_provenance_not_a_citable_source() -> None:
    failed = FetchResult(
        requested_url="https://example.gov/a",
        final_url="https://example.gov/a",
        status="blocked",
        ok=False,
        error="destination is not a public address",
        elapsed_ms=1.0,
    )

    items = to_evidence_items(failed, retrieved_by="researcher")

    assert len(items) == 1
    assert items[0].source_type == "other", "a failed fetch is never a web source"
    assert items[0].source_ref == "https://example.gov/a"
    assert "fetch status: blocked" in items[0].content
    assert "destination is not a public address" in items[0].content


def test_a_snippet_is_never_upgraded_to_page_text() -> None:
    result = WebResult(
        query="q",
        provider="searxng",
        max_results=1,
        elapsed_ms=1.0,
        results=[WebHit(title="T", url="https://example.gov/a", snippet="short")],
    )

    items = to_evidence_items(result, retrieved_by="researcher")

    assert items[0].content == "T\nshort"
    assert items[0].source_type == "web"
    assert items[0].score is None


def test_duplicate_search_urls_collapse_to_one_evidence_item() -> None:
    result = WebResult(
        query="q",
        provider="searxng",
        max_results=3,
        elapsed_ms=1.0,
        results=[
            WebHit(title="A", url="https://example.gov/a", snippet="one",
                   provider="searxng"),
            WebHit(title="A again", url="https://example.gov/a", snippet="two",
                   provider="searxng"),
            WebHit(title="B", url="https://example.gov/b", snippet="three",
                   provider="searxng"),
        ],
    )

    items = to_evidence_items(result, retrieved_by="researcher")

    assert [item.source_ref for item in items] == [
        "https://example.gov/a",
        "https://example.gov/b",
    ]
    assert len({item.id for item in items}) == 2


def test_the_hit_provider_wins_over_the_result_provider() -> None:
    """A fallback hit must not be recorded as a primary-provider result."""
    result = WebResult(
        query="q",
        provider="duckduckgo",
        max_results=1,
        elapsed_ms=1.0,
        note="primary provider (duckduckgo) unavailable; fallback provider (searxng) used",
        results=[
            WebHit(title="F", url="https://example.gov/f", snippet="s",
                   provider="searxng")
        ],
    )

    items = to_evidence_items(result, retrieved_by="researcher")

    assert items[0].provider == "searxng"


def test_unknown_result_type_is_rejected() -> None:
    class NotAResult:
        ok = True

    try:
        to_evidence_items(NotAResult(), retrieved_by="researcher")
    except TypeError as exc:
        assert "EvidenceItem" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("expected TypeError")


# ---------------------------------------------------------------- MCP surface


def _run(coro):
    return asyncio.run(coro)


def _registration(module) -> dict:
    """name -> published MCP tool object, read off the real FastMCP server."""
    tools = _run(module.mcp.list_tools())
    return {tool.name: tool for tool in tools}


EXPECTED_TOOLS = {
    "athena.tools.rag_server": ("rag_search",),
    "athena.tools.web_server": ("web_search", "fetch_page"),
    "athena.tools.calc_server": ("calculate", "forecast"),
}


def test_all_five_research_tools_are_registered_on_their_mcp_servers() -> None:
    import importlib

    registered: list[str] = []
    for module_name, expected in EXPECTED_TOOLS.items():
        module = importlib.import_module(module_name)
        names = tuple(_registration(module))
        assert names == expected, module_name
        registered.extend(names)

    assert sorted(registered) == sorted(
        ["rag_search", "web_search", "fetch_page", "calculate", "forecast"]
    ), "the registry and the MCP servers must expose the same five tools"


def test_published_schemas_carry_types_defaults_and_bounds() -> None:
    from athena.tools import calc_server, rag_server, web_server

    web = _registration(web_server)
    assert web["web_search"].inputSchema["required"] == ["query"]
    assert web["web_search"].inputSchema["properties"]["max_results"]["type"] == "integer"
    assert web["web_search"].inputSchema["properties"]["max_results"]["minimum"] == 1
    assert web["web_search"].inputSchema["properties"]["max_results"]["maximum"] == 20
    assert web["web_search"].inputSchema["properties"]["max_results"]["default"] == 5
    assert web["fetch_page"].inputSchema["properties"]["max_chars"]["minimum"] == 1000
    assert web["fetch_page"].inputSchema["properties"]["max_chars"]["maximum"] == 20000

    rag = _registration(rag_server)
    assert rag["rag_search"].inputSchema["properties"]["top_k"]["maximum"] == 20

    calc = _registration(calc_server)
    assert calc["calculate"].inputSchema["required"] == ["expression"]
    assert calc["forecast"].inputSchema["required"] == ["values", "horizon"]
    assert calc["forecast"].inputSchema["properties"]["values"]["items"]["type"] == "number"


def test_an_out_of_range_argument_is_rejected_instead_of_silently_clamped() -> None:
    from athena.tools import web_server

    with pytest.raises(Exception) as excinfo:
        _run(web_server.mcp.call_tool("web_search", {"query": "nasa", "max_results": 0}))

    assert "max_results" in str(excinfo.value)
    assert "greater_than_equal" in str(excinfo.value) or ">=" in str(excinfo.value)


def test_calculator_and_forecast_invoke_through_mcp_with_structured_output() -> None:
    from athena.tools import calc_server

    _, calc = _run(calc_server.mcp.call_tool("calculate", {"expression": "17.5 + 24.5"}))
    CalcResult.model_validate(calc)
    assert calc["ok"] is True and calc["value"] == 42.0
    assert calc["tool_name"] == "calculate"
    assert isinstance(calc["elapsed_ms"], float)

    _, forecast = _run(
        calc_server.mcp.call_tool("forecast", {"values": [1, 2, 3, 4], "horizon": 2})
    )
    ForecastResult.model_validate(forecast)
    assert forecast["ok"] is True and len(forecast["forecast"]) == 2


def test_a_tool_error_comes_back_as_a_result_not_an_exception() -> None:
    from athena.tools import calc_server

    _, bad = _run(calc_server.mcp.call_tool("calculate", {"expression": "open('f')"}))
    CalcResult.model_validate(bad)

    assert bad["ok"] is False
    assert "not allowed" in bad["error"]


def test_web_tools_normalize_failures_through_mcp(monkeypatch) -> None:
    from athena.tools import web_server

    monkeypatch.setenv("SEARCH_PROVIDER", "disabled")
    _, search = _run(web_server.mcp.call_tool("web_search", {"query": "nasa"}))
    WebResult.model_validate(search)
    assert search["ok"] is False
    assert "disabled" in search["error"]

    # A loopback destination is refused by the SSRF guard: still a result.
    _, fetch = _run(
        web_server.mcp.call_tool("fetch_page", {"url": "http://127.0.0.1/admin"})
    )
    FetchResult.model_validate(fetch)
    assert fetch["ok"] is False
    assert fetch["status"] == "blocked"
    assert fetch["fetch_id"], "provenance is per attempt, even for a refusal"


def test_rag_search_normalizes_an_unusable_index_through_mcp(monkeypatch) -> None:
    from athena.tools import rag_server
    from athena.tools.rag import search as rag_search_module

    def unavailable():
        raise RuntimeError("no index built")

    monkeypatch.setattr(rag_search_module, "get_index", unavailable)
    _, result = _run(rag_server.mcp.call_tool("rag_search", {"query": "orion"}))

    RagResult.model_validate(result)
    assert result["ok"] is False
    assert "no index built" in result["error"]
    assert result["hits"] == []


# --------------------------------------------------------------- diagnostics


SENTINEL_KEY = "sk-live-SENTINEL-must-never-appear-0987654321"


def test_provider_status_report_lists_providers_and_never_leaks(monkeypatch) -> None:
    from athena.tools import diagnostics

    monkeypatch.setenv("SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setenv("ATHENA_SEARXNG_URL", "http://user:pa55@127.0.0.1:8080/search?x=1")
    monkeypatch.setenv("LLM_MODEL", "groq/qwen/qwen3-32b")
    monkeypatch.setenv("LLM_API_KEY", SENTINEL_KEY)

    report = diagnostics.status_report()

    providers = {row["provider"]: row for row in report["search"]["providers"]}
    assert set(providers) == {"duckduckgo", "searxng"}
    assert providers["duckduckgo"]["roles"] == ["primary"]
    assert providers["searxng"]["roles"] == ["fallback"]
    assert providers["searxng"]["configured"] is True
    assert providers["searxng"]["reachable"] is None, "no probe was requested"
    assert "pa55" not in json.dumps(report), "URL userinfo is a credential"
    assert report["llm"]["configured"] is True
    assert report["llm"]["key_source"] == "env"
    assert diagnostics.leaks_secret(report, [SENTINEL_KEY, "pa55"]) == []


def test_provider_status_reports_the_last_error_class_only(monkeypatch) -> None:
    from athena.tools import diagnostics
    from athena.tools.web import search

    search.reset_search_health()
    monkeypatch.setenv("ATHENA_SEARXNG_URL", "http://127.0.0.1:8080")
    monkeypatch.setattr(
        search, "duckduckgo",
        lambda query, count: (_ for _ in ()).throw(search.SearchThrottled()),
    )
    monkeypatch.setattr(search, "searxng", lambda query, count, endpoint=None: [])
    search.web_search("nasa", 3)

    providers = {row["provider"]: row for row in diagnostics.search_providers()}
    assert providers["duckduckgo"]["last_error_class"] == "throttled"

    search.reset_search_health()
    providers = {row["provider"]: row for row in diagnostics.search_providers()}
    assert providers["duckduckgo"]["last_error_class"] == ""


def test_diagnostics_probes_a_local_searxng_without_network_assumptions(monkeypatch) -> None:
    """The probe path is exercised with the HTTP seam, so it stays deterministic."""
    from athena.tools import diagnostics
    from athena.tools.web import search

    monkeypatch.setenv("ATHENA_SEARXNG_URL", "http://127.0.0.1:8080")
    monkeypatch.setattr(
        search, "_fetch",
        lambda *a, **k: type("R", (), {
            "status_code": 200, "text": '{"results": []}',
            "json": lambda self=None: {"results": []},
        })(),
    )

    providers = {row["provider"]: row for row in diagnostics.search_providers(probe=True)}

    assert providers["searxng"]["reachable"] is True
    # The configured base URL is reported; the /search suffix is adapter detail.
    assert providers["searxng"]["endpoint"] == "http://127.0.0.1:8080"
    assert providers["searxng"]["last_error_class"] == ""
