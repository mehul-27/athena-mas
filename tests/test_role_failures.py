"""A failed analyst/critic must be loud, must write nothing, and must degrade."""

from __future__ import annotations

import asyncio
import json

import pytest

from athena.agents import crew_roles
from athena.agents.crew_server import run_role_request
from athena.blackboard import Blackboard, SQLiteStore, Task
from athena.observability.store import SQLiteEventStore
from athena.run_report import ack_warning
from athena.summary import RunSummary, finish, note_ack


@pytest.fixture()
def blackboard(tmp_path) -> Blackboard:
    return Blackboard.create(Task(question="q"), store=SQLiteStore(tmp_path / "bb.db"))


def _boom(*_args, **_kwargs):
    raise TypeError("Completions.parse() got an unexpected keyword argument")


def test_failed_analyst_writes_no_placeholder(blackboard: Blackboard) -> None:
    with pytest.raises(TypeError):
        crew_roles.analyse(
            blackboard.run_id, "go", store=blackboard.store, generate=_boom
        )

    assert blackboard.read("analysis") is None, "a failed analyst must not write"


def test_failed_critic_writes_no_placeholder(blackboard: Blackboard) -> None:
    with pytest.raises(TypeError):
        crew_roles.critique(
            blackboard.run_id, "go", store=blackboard.store, generate=_boom
        )

    assert blackboard.read("critique") is None, "a failed critic must not approve"


@pytest.mark.parametrize("role", ["analyst", "critic"])
def test_failed_role_returns_ok_false(blackboard: Blackboard, role: str) -> None:
    ack_text = run_role_request(
        json.dumps({"run_id": blackboard.run_id, "role": role, "instruction": "go"}),
        roles={role: _boom},
        store=blackboard.store,
    )
    ack = json.loads(ack_text)

    assert ack["ok"] is False
    assert ack["error_type"] == "TypeError"
    assert ack["role"] == role
    assert len(ack_text) < 300

    events = SQLiteEventStore().events(blackboard.run_id)
    errors = [event for event in events if event.kind == "error"]
    assert errors and errors[0].error_type == "TypeError"
    assert "unexpected keyword" in (errors[0].error_message or "")


@pytest.mark.parametrize("role", ["analyst", "critic"])
def test_failed_role_degrades_the_summary(blackboard: Blackboard, role: str) -> None:
    ack_text = run_role_request(
        json.dumps({"run_id": blackboard.run_id, "role": role, "instruction": "go"}),
        roles={role: _boom},
        store=blackboard.store,
    )

    summary = RunSummary(pattern="sequential")
    note_ack(summary, ack_text)
    finish(summary)

    assert summary.degraded is True
    assert summary.stage_errors == [f"{role}: TypeError"]
    assert ack_warning(ack_text).startswith(f"WARNING {role} failed: TypeError:")


def test_zero_token_success_is_flagged() -> None:
    ack = json.dumps(
        {"ok": True, "role": "analyst", "section": "analysis", "version": 1,
         "tokens_in": 0, "tokens_out": 0}
    )
    assert ack_warning(ack) == "WARNING analyst reported zero tokens"


def test_normal_success_is_quiet() -> None:
    ack = json.dumps(
        {"ok": True, "role": "writer", "section": "final_report", "version": 1,
         "tokens_in": 900, "tokens_out": 120}
    )
    assert ack_warning(ack) is None


def test_runner_prints_the_zero_token_warning(capsys) -> None:
    from test_run_execute import StubRunner, _event, _part

    import athena.run as runner_mod

    ack = json.dumps(
        {"ok": True, "role": "analyst", "section": "analysis", "version": 1,
         "tokens_in": 0, "tokens_out": 0}
    )
    stub = StubRunner([_event("analyst", parts=[_part(text=ack)])])

    asyncio.run(
        runner_mod._execute(
            "q", "sequential", verbose=True,
            runner_factory=lambda run_id, pattern, recorder: stub,
        )
    )

    assert "WARNING analyst reported zero tokens" in capsys.readouterr().out
