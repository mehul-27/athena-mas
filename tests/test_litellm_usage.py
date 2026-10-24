"""Offline tests for process-level usage capture (no network)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from athena.blackboard import Blackboard, SQLiteStore, Task
from athena.observability.litellm_usage import (
    TOTALS,
    usage_from_response,
    usage_scope,
)
from athena.observability.usage_hook import build_logger, install_usage_logger


def _response(prompt=10, completion=5, reasoning=None):
    details = {"reasoning_tokens": reasoning} if reasoning is not None else None
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            completion_tokens_details=details,
        )
    )


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    monkeypatch.setenv("ATHENA_USAGE_SETTLE_S", "0")


@pytest.fixture()
def logger():
    return build_logger()


@pytest.fixture(autouse=True)
def _restore_callbacks():
    import litellm

    before = list(getattr(litellm, "callbacks", None) or [])
    yield
    litellm.callbacks = before


def test_hook_in_the_same_thread_counts(logger) -> None:
    with usage_scope("analyst") as scope:
        logger.log_success_event({}, _response(10, 5, reasoning=2), None, None)

    assert scope.as_dict() == {"tokens_in": 10, "tokens_out": 5,
                                "reasoning_tokens": 2, "calls": 1}


def test_hook_from_another_thread_counts(logger) -> None:
    with usage_scope("writer") as scope:
        thread = threading.Thread(
            target=logger.log_success_event,
            args=({}, _response(40, 9), None, None),
        )
        thread.start()
        thread.join(timeout=5)

    assert (scope.tokens_in, scope.tokens_out, scope.calls) == (40, 9, 1)


def test_hook_from_an_asyncio_task_counts(logger) -> None:
    async def fire():
        await logger.async_log_success_event({}, _response(7, 3), None, None)

    async def main():
        with usage_scope("critic") as scope:
            await asyncio.create_task(fire())
        return scope

    scope = asyncio.run(main())
    assert (scope.tokens_in, scope.calls) == (7, 1)


def test_outside_a_scope_is_a_no_op(logger) -> None:
    logger.log_success_event({}, _response(10, 5), None, None)  # must not raise


def test_two_roles_back_to_back_get_their_own_deltas(logger) -> None:
    with usage_scope("analyst") as first:
        logger.log_success_event({}, _response(100, 20), None, None)
    with usage_scope("critic") as second:
        logger.log_success_event({}, _response(7, 2), None, None)

    assert first.as_dict()["tokens_in"] == 100
    assert second.as_dict() == {"tokens_in": 7, "tokens_out": 2,
                                "reasoning_tokens": 0, "calls": 1}


def test_late_callback_is_caught_by_the_settle_wait(logger, monkeypatch) -> None:
    monkeypatch.setenv("ATHENA_USAGE_SETTLE_S", "2")

    def late():
        time.sleep(0.1)
        logger.log_success_event({}, _response(5, 5), None, None)

    with usage_scope("writer") as scope:
        thread = threading.Thread(target=late)
        thread.start()
        thread.join(timeout=0)  # the call returns before the hook fires
    thread.join(timeout=5)

    assert scope.calls == 1
    assert scope.tokens_in == 5


def test_zero_settle_misses_the_late_callback(logger, monkeypatch) -> None:
    monkeypatch.setenv("ATHENA_USAGE_SETTLE_S", "0")

    def late():
        time.sleep(0.1)
        logger.log_success_event({}, _response(5, 5), None, None)

    with usage_scope("writer") as scope:
        thread = threading.Thread(target=late)
        thread.start()
        thread.join(timeout=0)
    thread.join(timeout=5)

    assert scope.calls == 0, "this is exactly why the settle wait exists"


def test_usage_shapes() -> None:
    assert usage_from_response(_response(4, 2, 9)) == (4, 2, 9)
    assert usage_from_response({"usage": {"prompt_tokens": 1, "completion_tokens": 2}}) == (1, 2, None)
    assert usage_from_response(SimpleNamespace()) == (0, 0, None)
    assert usage_from_response(None) == (0, 0, None)


def test_install_is_idempotent_and_reasserts(logger) -> None:
    import litellm

    assert install_usage_logger() is True
    assert install_usage_logger() is False
    installed = [cb for cb in litellm.callbacks if type(cb).__name__ == "UsageLogger"]
    assert len(installed) == 1

    # CrewAI resets the list when it builds an LLM (crewai/llm.py:707).
    litellm.callbacks = []
    assert install_usage_logger() is True
    assert len([cb for cb in litellm.callbacks if type(cb).__name__ == "UsageLogger"]) == 1


def test_ack_and_stage_event_carry_the_numbers(tmp_path, logger) -> None:
    from athena.agents.crew_server import run_role_request
    from athena.observability.metrics import agent_metrics
    from athena.observability.store import SQLiteEventStore

    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=store)

    def stub(run_id, instruction, store=None):
        with usage_scope("analyst") as scope:
            logger.log_success_event({}, _response(300, 40, reasoning=12), None, None)
            logger.log_success_event({}, _response(320, 44, reasoning=8), None, None)
        return {"section": "analysis", "version": 1, **scope.as_dict()}

    ack_text = run_role_request(
        json.dumps({"run_id": blackboard.run_id, "role": "analyst", "instruction": "go"}),
        roles={"analyst": stub},
        store=store,
    )
    ack = json.loads(ack_text)

    assert (ack["tokens_in"], ack["tokens_out"], ack["calls"]) == (620, 84, 2)
    assert ack["reasoning_tokens"] == 20
    assert "estimated" not in ack
    assert len(ack_text) < 300

    events = SQLiteEventStore().events(blackboard.run_id)
    stage_end = [event for event in events if event.kind == "stage_end"][0]
    assert (stage_end.tokens_in, stage_end.tokens_out) == (620, 84)
    assert stage_end.extra["calls"] == 2

    from athena.observability.events import Event

    call_event = Event(
        run_id=blackboard.run_id, kind="model_call", agent="analyst", framework="crewai",
        tokens_in=620, tokens_out=84, extra={"reasoning_tokens": 20, "calls": 2},
    )
    rows = agent_metrics([blackboard.run_id], {blackboard.run_id: [call_event]}, {})
    assert rows[0].tokens_in == 620
    assert rows[0].estimated_calls == 0


def test_global_totals_are_shared_across_threads(logger) -> None:
    before = TOTALS.snapshot()["calls"]

    def fire():
        logger.log_success_event({}, _response(1, 1), None, None)

    threads = [threading.Thread(target=fire) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert TOTALS.snapshot()["calls"] == before + 5
