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


def test_plugin_marks_a_typed_tool_failure_as_failed(store: SQLiteEventStore) -> None:
    recorder = Recorder("run-1", store)
    plugin = MonitoringPlugin(recorder)
    tool = SimpleNamespace(name="fetch_page")
    context = SimpleNamespace(agent_name="web_researcher")

    async def run() -> None:
        await plugin.before_tool_callback(
            tool=tool, tool_args={"url": "https://example.com"}, tool_context=context
        )
        await plugin.after_tool_callback(
            tool=tool, tool_args={"url": "https://example.com"}, tool_context=context,
            result={"structuredContent": {
                "ok": False, "status": "blocked", "error": "blocked by policy",
            }},
        )

    asyncio.run(run())

    event = store.events("run-1")[0]
    assert event.kind == "tool_call"
    assert event.ok is False
    assert event.error_type == "ToolError"
    assert "blocked by policy" in event.error_message


def test_parallel_calls_to_one_tool_keep_their_own_arguments_and_results(
    store: SQLiteEventStore,
) -> None:
    recorder = Recorder("run-parallel-tools", store)
    plugin = MonitoringPlugin(recorder)
    tool = SimpleNamespace(name="fetch_page")
    first = SimpleNamespace(agent_name="web_researcher", function_call_id="call-1")
    second = SimpleNamespace(agent_name="web_researcher", function_call_id="call-2")
    markdown = "[Docs](https://docs.example/a)"

    async def run() -> None:
        await plugin.before_tool_callback(
            tool=tool, tool_args={"url": markdown}, tool_context=first
        )
        await plugin.before_tool_callback(
            tool=tool, tool_args={"url": "https://docs.example/b"}, tool_context=second
        )
        await plugin.after_tool_callback(
            tool=tool,
            tool_args={"url": "https://docs.example/b"},
            tool_context=second,
            result={"structuredContent": {
                "ok": True, "status": "ok", "requested_url": "https://docs.example/b",
                "final_url": "https://docs.example/b",
            }},
        )
        await plugin.after_tool_callback(
            tool=tool,
            tool_args={"url": markdown},
            tool_context=first,
            result={"structuredContent": {
                "ok": True, "status": "ok", "requested_url": "https://docs.example/a",
                "final_url": "https://docs.example/a",
            }},
        )

    asyncio.run(run())

    events = store.events("run-parallel-tools")
    by_call = {event.extra["function_call_id"]: event for event in events}
    assert set(by_call) == {"call-1", "call-2"}
    assert "https://docs.example/a" in by_call["call-1"].payload_summary
    assert "]" not in by_call["call-1"].payload_summary
    assert "https://docs.example/b" in by_call["call-2"].payload_summary
    assert by_call["call-1"].duration_ms is not None
    assert by_call["call-2"].duration_ms is not None


def test_plugin_records_worker_block_reason_without_synthetic_duplicate(
    store: SQLiteEventStore,
) -> None:
    recorder = Recorder("run-block", store)
    plugin = MonitoringPlugin(recorder)
    context = SimpleNamespace(agent_name="calc_researcher", function_call_id="call-block")
    tool = SimpleNamespace(name="web_search")

    async def run() -> None:
        await plugin.before_tool_callback(
            tool=tool, tool_args={"query": "q"}, tool_context=context
        )
        await plugin.after_tool_callback(
            tool=tool,
            tool_args={"query": "q"},
            tool_context=context,
            result={
                "isError": True,
                "error": "Blocked: web_search is not assigned to calc_researcher.",
                "error_type": "ToolNotAssigned",
                "blocked_by": "worker_assignment",
            },
        )

    asyncio.run(run())

    events = store.events("run-block")
    assert len(events) == 1
    event = events[0]
    assert event.kind == "tool_call"
    assert event.ok is False
    assert event.error_type == "ToolNotAssigned"
    assert event.extra["blocked_by"] == "worker_assignment"
    assert "not assigned" in event.error_message


def test_stage_events_mark_wrapper_and_remote_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events.db"))
    store = SQLiteEventStore(tmp_path / "events.db")
    recorder = Recorder("run-scope", store)
    plugin = MonitoringPlugin(recorder)
    context = SimpleNamespace(agent_name="analyst")

    async def run() -> None:
        await plugin.before_agent_callback(
            agent=SimpleNamespace(name="analyst"), callback_context=context
        )
        await plugin.after_agent_callback(
            agent=SimpleNamespace(name="analyst"), callback_context=context
        )

    asyncio.run(run())
    events = store.events("run-scope")
    assert [event.extra.get("stage_scope") for event in events] == [
        "adk-wrapper", "adk-wrapper"
    ]

    blackboard = Blackboard.create(
        Task(question="q"), store=SQLiteStore(tmp_path / "bb.db")
    )

    def stub(_run_id, _instruction, store=None):
        return {"section": "analysis", "version": 1}

    run_role_request(
        json.dumps({"run_id": blackboard.run_id, "role": "analyst", "instruction": "go"}),
        roles={"analyst": stub},
        store=blackboard.store,
    )
    remote = SQLiteEventStore(tmp_path / "events.db").events(blackboard.run_id)
    assert all(event.extra.get("stage_scope") == "crewai-remote" for event in remote)
    assert all(event.extra.get("handoff") == "a2a" for event in remote)


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


def test_parallel_workers_with_the_same_display_name_both_finish(store) -> None:
    recorder = Recorder("run-1", store)
    set_recorder(recorder)
    plugin = MonitoringPlugin(recorder)
    context = SimpleNamespace(agent_name="web_researcher")
    first = SimpleNamespace(name="web_researcher")
    second = SimpleNamespace(name="web_researcher")

    async def run() -> None:
        await plugin.before_agent_callback(agent=first, callback_context=context)
        await plugin.before_agent_callback(agent=second, callback_context=context)
        await plugin.after_agent_callback(agent=first, callback_context=context)
        await plugin.after_agent_callback(agent=second, callback_context=context)

    asyncio.run(run())

    events = store.events("run-1")
    assert [event.kind for event in events] == [
        "stage_start", "stage_start", "stage_end", "stage_end"
    ]
    assert all(
        event.duration_ms is not None
        for event in events
        if event.kind == "stage_end"
    )


def test_monitoring_off_means_no_events(store: SQLiteEventStore) -> None:
    recorder = Recorder("run-off", store, enabled=False)
    recorder.emit("stage_start", "planner")
    recorder.emit("error", "planner", ok=False, error_message="x")
    assert store.events("run-off") == []


# --- the one-line emit helpers used by the retry/guardrail call sites -------
def test_emit_retry_writes_the_shared_retry_shape(store: SQLiteEventStore) -> None:
    """``emit_retry`` is the call site's whole job: one row, typed fields."""
    from athena.observability.hooks import emit_retry

    set_recorder(Recorder("run-emit", store))
    emit_retry(
        "writer", "the answer was cut off", "crewai",
        attempt=2, of=2, retry_type="truncated_report", stage="writer",
    )

    event = store.events("run-emit")[0]
    assert event.kind == "retry"
    assert event.agent == "writer"
    assert event.framework == "crewai"
    assert event.extra["retry_type"] == "truncated_report"
    assert event.extra["attempt"] == 2 and event.extra["of"] == 2
    assert event.extra["stage"] == "writer"
    assert event.extra["reason"] == "the answer was cut off"
    assert "inferred" not in event.extra


def test_emit_retry_is_silent_with_no_recorder(store: SQLiteEventStore) -> None:
    from athena.observability.hooks import emit_retry

    set_recorder(None)
    emit_retry("writer", "nothing happened", "crewai", retry_type="other")
    assert store.events("run-emit") == []


def test_emit_guardrail_redacts_and_labels_the_stage(store: SQLiteEventStore) -> None:
    from athena.observability.hooks import emit_guardrail

    set_recorder(Recorder("run-emit", store))
    emit_guardrail(
        "writer", "report.unsupported_claim", "sanitized",
        "removed 1 unsupported claim(s)", framework="crewai",
        stage="report_sanitize",
    )

    event = store.events("run-emit")[0]
    assert event.kind == "guardrail"
    assert event.extra["stage"] == "report_sanitize"
    assert event.extra["rule"] == "report.unsupported_claim"
    assert event.extra["action"] == "sanitized"
    assert event.extra["screen"] == "emit_guardrail"
    assert event.ok is True


def test_emit_guardrail_marks_a_blocked_action_as_not_ok(store: SQLiteEventStore) -> None:
    from athena.observability.hooks import emit_guardrail

    set_recorder(Recorder("run-emit", store))
    emit_guardrail("guardrails", "interagent.signature", "blocked", "signature mismatch")

    assert store.events("run-emit")[0].ok is False
