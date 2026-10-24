"""Offline tests for metrics, the heatmap and the provider-key mapping."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from athena.observability.events import Event
from athena.observability.heatmap import render_heatmap
from athena.observability.metrics import agent_metrics, format_metrics


def _events() -> list[Event]:
    return [
        Event(run_id="r1", kind="stage_end", agent="planner", framework="adk", duration_ms=100),
        Event(run_id="r1", kind="model_call", agent="planner", framework="adk",
              tokens_in=40, tokens_out=10),
        Event(run_id="r1", kind="stage_end", agent="writer", framework="crewai", duration_ms=300),
        Event(run_id="r1", kind="model_call", agent="writer", framework="crewai",
              tokens_in=500, tokens_out=150),
        Event(run_id="r1", kind="a2a_call", agent="writer", framework="adk",
              extra={"request_bytes": 156, "response_bytes": 90}),
        Event(run_id="r1", kind="tool_call", agent="researcher", framework="adk"),
        Event(run_id="r1", kind="limiter_wait", agent="writer", framework="crewai", duration_ms=60000),
        Event(run_id="r1", kind="error", agent="critic", framework="crewai", ok=False,
              error_message="boom"),
    ]


def test_metrics_match_hand_computed_values() -> None:
    events = _events()
    rows = agent_metrics(
        ["r1"], {"r1": events}, {"r1": ["researcher", "researcher"]}
    )
    by_agent = {row.agent: row for row in rows}

    assert by_agent["planner"].stage_ms == 100
    assert by_agent["planner"].model_calls == 1
    assert by_agent["planner"].tokens_in == 40

    assert by_agent["writer"].tokens_out == 150
    assert by_agent["writer"].a2a_bytes == 246
    assert by_agent["writer"].limiter_wait_ms == 60000
    assert by_agent["researcher"].tool_calls == 1
    assert by_agent["researcher"].evidence_items == 2
    assert by_agent["critic"].errors == 1
    assert by_agent["writer"].means()["tokens_in"] == 500.0


def test_metrics_sum_across_runs() -> None:
    events = _events()
    rows = agent_metrics(["r1", "r1"], {"r1": events}, {})
    planner = next(row for row in rows if row.agent == "planner")
    assert planner.stage_ms == 200
    assert planner.runs == 2
    assert planner.means()["stage_ms"] == 100.0


def test_metrics_table_renders() -> None:
    rows = agent_metrics(["r1"], {"r1": _events()}, {})
    text = format_metrics(rows)
    assert "agent" in text and "tok_out" in text
    assert "writer" in text


def test_heatmap_writes_a_png_and_a_csv(tmp_path) -> None:
    rows = agent_metrics(["r1"], {"r1": _events()}, {"r1": ["researcher"]})
    target = tmp_path / "heatmap.png"

    written = render_heatmap(rows, target, "Athena sequential - 1 run(s)")

    assert written == target
    assert target.exists() and target.stat().st_size > 1000
    assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    with target.with_suffix(".csv").open(newline="", encoding="utf-8") as handle:
        table = list(csv.DictReader(handle))
    writer_row = next(row for row in table if row["agent"] == "writer")
    assert int(float(writer_row["tokens_out"])) == 150
    assert int(float(writer_row["a2a_bytes"])) == 246


def test_provider_key_mapping(monkeypatch) -> None:
    from athena.config import llm

    cases = {
        "groq/qwen/qwen3.8-27b": "GROQ_API_KEY",
        "deepseek/deepseek-chat": "DEEPSEEK_API_KEY",
        "openai/gpt-4o-mini": "OPENAI_API_KEY",
        "gemini/gemini-2.0-flash": "GEMINI_API_KEY",
        "anthropic/claude-3": "ANTHROPIC_API_KEY",
        "openrouter/meta/llama": "OPENROUTER_API_KEY",
        "together_ai/llama": "TOGETHERAI_API_KEY",
        "mistral/mistral-large": "MISTRAL_API_KEY",
    }
    for model, expected in cases.items():
        assert llm.provider_key_var(model) == expected

    assert llm.provider_key_var("unknown/model") is None


def test_export_provider_key_does_not_overwrite(monkeypatch) -> None:
    from athena.config import llm

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert llm.export_provider_key("secret-value", "groq/x") == "GROQ_API_KEY"
    assert __import__("os").environ["GROQ_API_KEY"] == "secret-value"

    monkeypatch.setenv("GROQ_API_KEY", "already-set")
    llm.export_provider_key("new-value", "groq/x")
    assert __import__("os").environ["GROQ_API_KEY"] == "already-set"

    assert llm.export_provider_key("x", "unknown/model") == ""


def test_api_base_is_optional(monkeypatch) -> None:
    from athena.config import llm

    monkeypatch.delenv("LLM_API_BASE", raising=False)
    assert llm.api_base() == ""
    monkeypatch.setenv("LLM_API_BASE", "http://localhost:8000/v1")
    assert llm.api_base() == "http://localhost:8000/v1"
