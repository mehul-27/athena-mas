"""Offline tests for the empty-reply retry and reasoning-token accounting."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from athena.blackboard import Blackboard, SQLiteStore, Task
from athena.config import llm as llm_config
from athena.config.limited_models import RateLimitedLiteLlm
from athena.config.reply_guard import MAX_RETRY_TOKENS, doubled
from athena.observability.adk_plugin import MonitoringPlugin
from athena.observability.events import Event
from athena.observability.metrics import agent_metrics
from athena.observability.recorder import Recorder, set_recorder
from athena.observability.store import SQLiteEventStore


def _usage(prompt=10, completion=5, reasoning=None):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=completion,
        thoughts_token_count=reasoning,
    )


def _response(text=None, finish="STOP", usage=None):
    parts = [SimpleNamespace(text=text, thought=False)] if text else []
    return SimpleNamespace(
        content=SimpleNamespace(parts=parts),
        finish_reason=finish,
        usage_metadata=usage or _usage(),
    )


def _fake_runner(script, seen: list):
    """Patch target for LiteLlm.generate_content_async (a plain function binds)."""

    def fake(self, llm_request, stream=False):
        seen.append(self._additional_args.get("max_tokens"))
        payload = script[min(len(seen) - 1, len(script) - 1)]

        async def gen():
            for response in payload:
                yield response

        return gen()

    return fake


@pytest.fixture()
def events(tmp_path) -> SQLiteEventStore:
    store = SQLiteEventStore(tmp_path / "events.db")
    set_recorder(Recorder("run-r", store))
    yield store
    set_recorder(None)


def _model(max_tokens=100) -> RateLimitedLiteLlm:
    return RateLimitedLiteLlm(model="groq/test", api_key="x", max_tokens=max_tokens)


def _collect(model) -> list:
    async def run():
        return [item async for item in model.generate_content_async(SimpleNamespace())]

    return asyncio.run(run())


def test_empty_reply_is_retried_with_a_doubled_budget(monkeypatch, events) -> None:
    from google.adk.models.lite_llm import LiteLlm

    seen: list = []
    monkeypatch.setattr(
        LiteLlm, "generate_content_async",
        _fake_runner([[_response(text=None, finish="LENGTH")], [_response(text="hello")]], seen),
    )
    model = _model(100)

    responses = _collect(model)

    assert [r.content.parts[0].text for r in responses if r.content.parts] == ["hello"]
    assert seen == [100, 200]
    assert model._additional_args["max_tokens"] == 100  # restored
    assert len([e for e in events.events("run-r") if e.error_type == "empty_reply"]) == 1


def test_empty_reply_twice_does_not_loop(monkeypatch, events) -> None:
    from google.adk.models.lite_llm import LiteLlm

    seen: list = []
    monkeypatch.setattr(
        LiteLlm, "generate_content_async",
        _fake_runner([[_response(text=None, finish="LENGTH")]], seen),
    )

    responses = _collect(_model(100))

    assert seen == [100, 200]  # one retry, then stop
    assert all(not r.content.parts for r in responses)
    assert len([e for e in events.events("run-r") if e.error_type == "empty_reply"]) == 2


def test_doubled_respects_the_cap() -> None:
    assert doubled(100) == 200
    assert doubled(3000) == MAX_RETRY_TOKENS
    assert doubled(None) == 2000


def test_crewai_empty_reply_is_retried(monkeypatch, events) -> None:
    from crewai import LLM

    llm = llm_config.get_crewai_llm("writer")
    budgets: list = []

    def fake_call(self, **kwargs):
        budgets.append(self.max_tokens)
        return "" if len(budgets) == 1 else "an answer"

    monkeypatch.setattr(LLM, "call", fake_call)

    assert llm.call("prompt") == "an answer"
    assert budgets == [3000, MAX_RETRY_TOKENS]
    assert len([e for e in events.events("run-r") if e.error_type == "empty_reply"]) == 1


def test_reasoning_tokens_reach_the_event(events) -> None:
    plugin = MonitoringPlugin(Recorder("run-r", events))

    asyncio.run(
        plugin.after_model_callback(
            callback_context=SimpleNamespace(agent_name="planner"),
            llm_response=SimpleNamespace(usage_metadata=_usage(11, 3, reasoning=42)),
        )
    )

    event = events.events("run-r")[0]
    assert event.extra["reasoning_tokens"] == 42
    assert event.tokens_out == 3  # total completion, reasoning included


def test_missing_reasoning_tokens_is_none(events) -> None:
    plugin = MonitoringPlugin(Recorder("run-r", events))
    asyncio.run(
        plugin.after_model_callback(
            callback_context=SimpleNamespace(agent_name="planner"),
            llm_response=SimpleNamespace(usage_metadata=_usage()),
        )
    )
    assert events.events("run-r")[0].extra["reasoning_tokens"] is None


def test_reasoning_tokens_in_the_ack_stay_short(events, tmp_path) -> None:
    from athena.agents.crew_server import run_role_request

    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=store)

    def stub(run_id, instruction, store=None):
        return {"section": "analysis", "version": 1, "tokens_in": 10,
                "tokens_out": 5, "reasoning_tokens": 7, "model_ms": 100}

    ack_text = run_role_request(
        json.dumps({"run_id": blackboard.run_id, "role": "analyst", "instruction": "go"}),
        roles={"analyst": stub},
        store=store,
    )
    ack = json.loads(ack_text)

    assert ack["reasoning_tokens"] == 7
    assert len(ack_text) < 300


def test_metrics_and_csv_carry_reasoning_tokens(tmp_path) -> None:
    from athena.observability.heatmap import render_heatmap

    events = [
        Event(run_id="r", kind="model_call", agent="writer", framework="crewai",
              tokens_in=100, tokens_out=80, extra={"reasoning_tokens": 55}),
        Event(run_id="r", kind="model_call", agent="critic", framework="crewai",
              tokens_in=10, tokens_out=5),
    ]
    rows = agent_metrics(["r"], {"r": events}, {})
    by_agent = {row.agent: row for row in rows}
    assert by_agent["writer"].reasoning_tokens == 55
    assert by_agent["critic"].reasoning_tokens == 0  # missing field, no crash

    target = tmp_path / "heat.png"
    render_heatmap(rows, target, "reasoning check")
    header = target.with_suffix(".csv").read_text(encoding="utf-8").splitlines()[0]
    assert "reasoning_tokens" in header
