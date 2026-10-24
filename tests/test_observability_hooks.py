"""Offline tests for the callbacks that emit events."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from athena.agents import a2a_client
from athena.agents.a2a_stage import A2AStageAgent
from athena.agents.crew_server import run_role_request
from athena.blackboard import Blackboard, SQLiteStore, Task, set_write_listener
from athena.observability.adk_plugin import MonitoringPlugin
from athena.observability.hooks import install_hooks
from athena.observability.recorder import Recorder, set_recorder
from athena.observability.store import SQLiteEventStore


@pytest.fixture()
def store(tmp_path) -> SQLiteEventStore:
    return SQLiteEventStore(tmp_path / "events.db")


@pytest.fixture(autouse=True)
def _reset_recorder():
    yield
    set_recorder(None)
    set_write_listener(None)


def _usage(prompt: int, completion: int):
    return SimpleNamespace(prompt_token_count=prompt, candidates_token_count=completion)


def test_plugin_emits_stage_and_model_events(store: SQLiteEventStore) -> None:
    recorder = Recorder("run-1", store)
    set_recorder(recorder)
    plugin = MonitoringPlugin(recorder)
    context = SimpleNamespace(agent_name="planner")

    async def run() -> None:
        await plugin.before_agent_callback(
            agent=SimpleNamespace(name="planner"), callback_context=context
        )
        await plugin.after_model_callback(
            callback_context=context, llm_response=SimpleNamespace(usage_metadata=_usage(11, 3))
        )
        await plugin.after_agent_callback(
            agent=SimpleNamespace(name="planner"), callback_context=context
        )

    asyncio.run(run())

    events = store.events("run-1")
    assert [event.kind for event in events] == ["stage_start", "model_call", "stage_end"]
    assert events[1].tokens_in == 11
    assert events[1].tokens_out == 3
    assert events[2].tokens_in == 11  # summed on the stage
    assert events[2].duration_ms is not None


def test_plugin_emits_tool_call_and_error(store: SQLiteEventStore) -> None:
    recorder = Recorder("run-1", store)
    plugin = MonitoringPlugin(recorder)
    tool = SimpleNamespace(name="rag_search")
    context = SimpleNamespace(agent_name="researcher")

    async def run() -> None:
        await plugin.before_tool_callback(
            tool=tool, tool_args={"query": "nasa"}, tool_context=context
        )
        await plugin.after_tool_callback(
            tool=tool, tool_args={"query": "nasa"}, tool_context=context,
            result={"structuredContent": {"hits": []}},
        )
        await plugin.on_model_error_callback(
            context, None, RuntimeError("rate limited for gsk_abcdefghijklmnop")
        )

    asyncio.run(run())

    events = store.events("run-1")
    kinds = [event.kind for event in events]
    assert kinds == ["tool_call", "error"]
    assert events[0].payload_summary.startswith("rag_search")
    assert events[0].duration_ms is not None
    assert events[1].ok is False
    assert "gsk_" not in (events[1].error_message or "")


def test_crewai_ack_carries_tokens_and_stays_short(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events.db"))
    bb_store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=bb_store)

    def stub(run_id, instruction, store=None):
        return {"section": "analysis", "version": 1,
                "tokens_in": 123, "tokens_out": 45, "model_ms": 900}

    ack_text = run_role_request(
        json.dumps({"run_id": blackboard.run_id, "role": "analyst", "instruction": "go"}),
        roles={"analyst": stub},
        store=bb_store,
    )
    ack = json.loads(ack_text)

    assert ack["tokens_in"] == 123
    assert ack["tokens_out"] == 45
    assert len(ack_text) < 300
    events = SQLiteEventStore(tmp_path / "events.db").events(blackboard.run_id)
    kinds = [event.kind for event in events]
    assert "stage_start" in kinds and "stage_end" in kinds


def test_a2a_stage_emits_bytes(store: SQLiteEventStore) -> None:
    set_recorder(Recorder("run-a2a", store))
    agent = A2AStageAgent(
        name="analyst", role="analyst", run_id="run-a2a",
        instruction="go", card_url="http://127.0.0.1:9001",
    )
    a2a_client._LAST.update({"out": 156, "in": 80})

    agent._record_call(json.dumps({"ok": True, "tokens_in": 7, "tokens_out": 2}), time.perf_counter())

    event = store.events("run-a2a")[0]
    assert event.kind == "a2a_call"
    assert event.extra["request_bytes"] == 156
    assert event.extra["response_bytes"] == 80
    assert event.tokens_in == 7


def test_blackboard_writes_emit_events(store: SQLiteEventStore, tmp_path) -> None:
    blackboard = Blackboard.create(Task(question="q"), store=SQLiteStore(tmp_path / "bb.db"))
    set_recorder(Recorder(blackboard.run_id, store))
    install_hooks()

    blackboard.write("critique", __import__("athena.blackboard", fromlist=["Critique"]).Critique(
        issues=[], severity="low", approved=True), author="critic")

    events = store.events(blackboard.run_id)
    assert events[-1].kind == "blackboard_write"
    assert events[-1].agent == "critic"
    assert events[-1].framework == "crewai"
    assert events[-1].extra["section"] == "critique"


def test_limiter_wait_is_recorded(store: SQLiteEventStore, tmp_path) -> None:
    from athena.config.ratelimit import TokenLimiter, set_wait_listener

    set_recorder(Recorder("run-wait", store))
    recorded: list = []
    set_wait_listener(lambda ms, tokens, agent: recorded.append((ms, agent)))

    class Clock:
        def __init__(self): self.now = 1000.0
        def __call__(self): return self.now
        def sleep(self, seconds): self.now += seconds

    clock = Clock()
    limiter = TokenLimiter(tmp_path / "rl.db", limit=100, clock=clock, sleep=clock.sleep)
    limiter.acquire(100)
    limiter.acquire(50, agent="writer")

    assert recorded and recorded[0][1] == "writer"
    set_wait_listener(None)


def test_monitoring_off_means_no_events(store: SQLiteEventStore) -> None:
    recorder = Recorder("run-off", store, enabled=False)
    recorder.emit("stage_start", "planner")
    recorder.emit("error", "planner", ok=False, error_message="x")
    assert store.events("run-off") == []
