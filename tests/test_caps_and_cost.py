"""Per-pattern caps, honest cost basis, and the estimate flag in the ack."""

from __future__ import annotations

import asyncio
import json

import pytest

from athena.config.caps import PATTERN_CAPS, cap_message, llm_call_cap
from athena.observability.store import SQLiteEventStore
from athena.summary import RunSummary, finish, note_ack, summary_line


def test_per_pattern_defaults(monkeypatch) -> None:
    monkeypatch.delenv("ATHENA_MAX_LLM_CALLS", raising=False)

    assert llm_call_cap("sequential") == 12
    assert llm_call_cap("p2p") == 12
    assert llm_call_cap("parallel") == 30
    assert llm_call_cap("hierarchical") == 40
    assert set(PATTERN_CAPS) == {"sequential", "p2p", "parallel", "hierarchical"}


def test_env_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("ATHENA_MAX_LLM_CALLS", "7")
    for pattern in PATTERN_CAPS:
        assert llm_call_cap(pattern) == 7


def test_cap_message_names_the_pattern_and_variable() -> None:
    message = cap_message("parallel", 30)

    assert "cap 30" in message
    assert "parallel" in message
    assert "ATHENA_MAX_LLM_CALLS" in message


def test_cap_reached_marks_the_run_failed(monkeypatch, capsys) -> None:
    from athena import run as runner

    monkeypatch.delenv("ATHENA_MAX_LLM_CALLS", raising=False)
    summary = runner._cap_reached("parallel")

    printed = capsys.readouterr().err
    assert "Run stopped: LLM call cap 30 reached for pattern parallel" in printed
    assert "Traceback" not in printed
    assert summary.failed is True
    assert summary.degraded is True

    events = SQLiteEventStore().events("cap-parallel")
    assert events and events[0].error_type == "LlmCallsLimitExceededError"


def test_run_one_returns_a_failed_summary_on_the_cap(monkeypatch) -> None:
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError

    from athena import run as runner

    async def boom(*_args, **_kwargs):
        raise LlmCallsLimitExceededError("cap")

    monkeypatch.setattr(runner, "_execute", boom)
    summary = asyncio.run(runner.run_one("q", "sequential"))

    assert summary.failed is True


def test_cost_basis_line() -> None:
    measured = RunSummary(pattern="sequential")
    finish(measured)
    assert "cost basis: measured" in summary_line(measured)

    estimated = RunSummary(pattern="sequential")
    note_ack(estimated, json.dumps({"ok": True, "role": "writer",
                                    "tokens_in": 10, "tokens_out": 5,
                                    "estimated": True}))
    finish(estimated)
    assert estimated.estimated is True
    assert "cost basis: partly estimated" in summary_line(estimated)


def test_server_ack_carries_the_estimate_flag(tmp_path) -> None:
    from athena.agents.crew_server import run_role_request
    from athena.blackboard import Blackboard, SQLiteStore, Task

    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=store)

    def stub(run_id, instruction, store=None):
        return {"section": "analysis", "version": 1, "tokens_in": 40,
                "tokens_out": 12, "reasoning_tokens": 0, "calls": 0,
                "estimated": True}

    ack_text = run_role_request(
        json.dumps({"run_id": blackboard.run_id, "role": "analyst", "instruction": "go"}),
        roles={"analyst": stub},
        store=store,
    )
    ack = json.loads(ack_text)

    assert ack["estimated"] is True
    assert len(ack_text) < 300
    assert "estimated" in ack_text


def test_server_ack_omits_the_flag_when_measured(tmp_path) -> None:
    from athena.agents.crew_server import run_role_request
    from athena.blackboard import Blackboard, SQLiteStore, Task

    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=store)

    def stub(run_id, instruction, store=None):
        return {"section": "analysis", "version": 1, "tokens_in": 400,
                "tokens_out": 120, "reasoning_tokens": 9, "calls": 1}

    ack = json.loads(run_role_request(
        json.dumps({"run_id": blackboard.run_id, "role": "analyst", "instruction": "go"}),
        roles={"analyst": stub},
        store=store,
    ))

    assert "estimated" not in ack


@pytest.mark.parametrize("pattern", list(PATTERN_CAPS))
def test_cap_is_positive(pattern: str) -> None:
    assert llm_call_cap(pattern) > 0
