"""Offline tests for Step 6c: native-OpenAI path, loud failures, ADK reasoning."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.agents import a2a_client
from athena.agents.a2a_stage import A2AStageAgent
from athena.agents.crew_server import run_role_request
from athena.blackboard import Blackboard, SQLiteStore, Task
from athena.config import llm as llm_config
from athena.observability.adk_plugin import MonitoringPlugin
from athena.observability.metrics import agent_metrics
from athena.observability.recorder import Recorder, set_recorder
from athena.observability.store import SQLiteEventStore
from athena.summary import RunSummary, finish, note_ack, summary_line

MODELS = ["openai/some-org/some-model", "groq/x", "deepseek/x"]


# --- Part 1: the CrewAI LLM must fit its path -------------------------------


@pytest.mark.parametrize("model", MODELS)
def test_crewai_llm_stays_on_the_litellm_path(monkeypatch, model: str) -> None:
    monkeypatch.setenv("LLM_MODEL", model)
    llm = llm_config.get_crewai_llm("writer")

    assert llm.is_litellm is True, "native client would reject LiteLLM-only kwargs"
    assert llm.additional_params.get("num_retries") == llm_config.MAX_RETRIES


@pytest.mark.parametrize("model", MODELS)
def test_custom_settings_survive_on_the_chosen_path(monkeypatch, model: str) -> None:
    monkeypatch.setenv("LLM_MODEL", model)
    monkeypatch.setenv("LLM_API_BASE", "http://gateway.local:8080/v1")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")

    llm = llm_config.get_crewai_llm("writer")

    assert llm.base_url == "http://gateway.local:8080/v1"
    assert llm.max_tokens == llm_config.ROLE_MAX_TOKENS["writer"]
    assert llm.reasoning_effort == "high"


def test_native_path_gets_max_retries_not_num_retries() -> None:
    """Regression: num_retries is a LiteLLM kwarg and breaks the OpenAI SDK."""

    class NativeLLM:
        is_litellm = False
        max_retries = 2
        additional_params: dict = {}

    native = NativeLLM()
    llm_config._apply_retries(native)

    assert native.max_retries == llm_config.MAX_RETRIES
    assert "num_retries" not in native.additional_params


def test_litellm_path_gets_num_retries() -> None:
    class LiteLLMish:
        is_litellm = True
        max_retries = 2
        additional_params: dict = {}

    litellm_llm = LiteLLMish()
    llm_config._apply_retries(litellm_llm)

    assert litellm_llm.additional_params["num_retries"] == llm_config.MAX_RETRIES
    assert litellm_llm.max_retries == 2


# --- Part 2: failures are loud ---------------------------------------------


@pytest.fixture()
def blackboard(tmp_path) -> Blackboard:
    return Blackboard.create(Task(question="q"), store=SQLiteStore(tmp_path / "bb.db"))


def _request(blackboard: Blackboard, role: str = "analyst") -> str:
    return json.dumps({"run_id": blackboard.run_id, "role": role, "instruction": "go"})


def test_role_exception_returns_a_failure_ack(blackboard: Blackboard) -> None:
    def boom(run_id, instruction, store=None):
        raise TypeError("Completions.parse() got an unexpected keyword argument")

    ack_text = run_role_request(
        _request(blackboard), roles={"analyst": boom}, store=blackboard.store
    )
    ack = json.loads(ack_text)

    assert ack["ok"] is False
    assert ack["role"] == "analyst"
    assert ack["error_type"] == "TypeError"
    assert "unexpected keyword" in ack["error"]
    assert len(ack_text) < 300

    error_events = [
        event for event in SQLiteEventStore().events(blackboard.run_id)
        if event.kind == "error"
    ]
    assert error_events and error_events[0].error_type == "TypeError"
    assert "unexpected keyword" in (error_events[0].error_message or "")


def test_failure_ack_redacts_a_key(blackboard: Blackboard, monkeypatch) -> None:
    monkeypatch.setenv("SOME_API_KEY", "gsk_live_abcdefghijklmnop1234")

    def boom(run_id, instruction, store=None):
        raise RuntimeError("auth failed for gsk_live_abcdefghijklmnop1234")

    ack = json.loads(
        run_role_request(_request(blackboard), roles={"analyst": boom},
                         store=blackboard.store)
    )

    assert "gsk_live" not in ack["error"]
    assert "[REDACTED]" in ack["error"]
    stored = SQLiteEventStore().events(blackboard.run_id)[0]
    assert "gsk_live" not in (stored.error_message or "")


def test_a2a_stage_emits_error_on_failure(blackboard: Blackboard) -> None:
    set_recorder(Recorder(blackboard.run_id, SQLiteEventStore()))
    agent = A2AStageAgent(
        name="analyst", role="analyst", run_id=blackboard.run_id,
        instruction="go", card_url="http://127.0.0.1:9001",
    )
    a2a_client._LAST.update({"out": 156, "in": 90})
    ack = json.dumps({"ok": False, "role": "analyst", "error_type": "TypeError",
                      "error": "bad kwarg"})

    agent._record_call(ack, time.perf_counter())

    kinds = [(e.kind, e.error_type) for e in SQLiteEventStore().events(blackboard.run_id)]
    assert ("error", "TypeError") in kinds
    set_recorder(None)


def test_summary_marks_a_run_degraded() -> None:
    summary = RunSummary(pattern="sequential")

    note_ack(summary, json.dumps({"ok": False, "role": "writer", "error_type": "TypeError"}))
    note_ack(summary, json.dumps({"ok": True, "role": "critic", "fallback": True}))
    finish(summary)

    assert summary.stage_errors == ["writer: TypeError"]
    assert summary.degraded is True
    assert "degraded True" in summary_line(summary)
    assert summary.row()[-1] == "yes"


def test_summary_stays_healthy_without_failures() -> None:
    summary = RunSummary(pattern="sequential")
    note_ack(summary, json.dumps({"ok": True, "role": "analyst", "version": 1}))
    finish(summary)
    assert summary.degraded is False
    assert summary.row()[-1] == "no"


# --- Part 3: ADK reasoning tokens -------------------------------------------


def test_reasoning_tokens_from_completion_details_reach_the_event(
    blackboard: Blackboard,
) -> None:
    store = SQLiteEventStore()
    plugin = MonitoringPlugin(Recorder(blackboard.run_id, store))
    usage = SimpleNamespace(
        prompt_token_count=12,
        candidates_token_count=40,
        thoughts_token_count=None,
        completion_tokens_details={"reasoning_tokens": 36},
    )

    asyncio.run(
        plugin.after_model_callback(
            callback_context=SimpleNamespace(agent_name="planner"),
            llm_response=SimpleNamespace(usage_metadata=usage),
        )
    )

    event = store.events(blackboard.run_id)[0]
    assert event.extra["reasoning_tokens"] == 36

    rows = agent_metrics([blackboard.run_id], {blackboard.run_id: [event]}, {})
    assert rows[0].reasoning_tokens == 36


# --- Part 4: test isolation -------------------------------------------------


def test_default_event_database_is_not_touched() -> None:
    default = Path("data/events.db")
    before = default.stat().st_mtime_ns if default.exists() else None

    recorder = Recorder.for_run("isolation-check")
    recorder.emit("stage_start", "planner")

    assert Path(SQLiteEventStore().path).resolve() != default.resolve()
    after = default.stat().st_mtime_ns if default.exists() else None
    assert after == before
