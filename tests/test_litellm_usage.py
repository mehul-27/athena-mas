"""Offline tests for litellm-layer usage capture (no network)."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from athena.blackboard import Blackboard, SQLiteStore, Task
from athena.observability.litellm_usage import (
    build_logger,
    install_usage_logger,
    run_in_scope,
    usage_from_response,
    usage_scope,
)


def _response(prompt=10, completion=5, reasoning=None):
    details = {"reasoning_tokens": reasoning} if reasoning is not None else None
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            completion_tokens_details=details,
        )
    )


@pytest.fixture()
def logger():
    return build_logger()


@pytest.fixture(autouse=True)
def _restore_callbacks():
    import litellm

    before = list(getattr(litellm, "callbacks", None) or [])
    yield
    litellm.callbacks = before


def test_object_dict_and_missing_usage(logger) -> None:
    with usage_scope("analyst") as scope:
        logger.log_success_event({}, _response(10, 5, reasoning=3), None, None)
        logger.log_success_event({}, {"usage": {"prompt_tokens": 7, "completion_tokens": 2}}, None, None)
        logger.log_success_event({}, SimpleNamespace(), None, None)

    assert scope.tokens_in == 17
    assert scope.tokens_out == 7
    assert scope.reasoning_tokens == 3
    assert scope.calls == 3


def test_outside_a_scope_is_a_no_op(logger) -> None:
    logger.log_success_event({}, _response(10, 5), None, None)  # must not raise


def test_usage_shapes() -> None:
    assert usage_from_response(_response(4, 2, 9)) == (4, 2, 9)
    assert usage_from_response({"usage": {"prompt_tokens": 1, "completion_tokens": 2}}) == (1, 2, None)
    assert usage_from_response(SimpleNamespace()) == (0, 0, None)
    assert usage_from_response(None) == (0, 0, None)


def test_two_threads_do_not_mix(logger) -> None:
    results: dict[str, dict] = {}
    barrier = threading.Barrier(2)

    def worker(role: str, calls: int) -> None:
        with usage_scope(role) as scope:
            barrier.wait(timeout=5)
            for _ in range(calls):
                logger.log_success_event({}, _response(2, 1), None, None)
            results[role] = scope.as_dict()

    threads = [
        threading.Thread(target=worker, args=("analyst", 2)),
        threading.Thread(target=worker, args=("critic", 5)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert results["analyst"] == {"tokens_in": 4, "tokens_out": 2,
                                  "reasoning_tokens": 0, "calls": 2}
    assert results["critic"]["calls"] == 5
    assert results["critic"]["tokens_in"] == 10


def test_two_asyncio_tasks_do_not_mix(logger) -> None:
    async def run(role: str, calls: int) -> dict:
        with usage_scope(role) as scope:
            await asyncio.sleep(0)
            for _ in range(calls):
                await logger.async_log_success_event({}, _response(3, 2), None, None)
            await asyncio.sleep(0)
            return scope.as_dict()

    async def main():
        return await asyncio.gather(run("analyst", 1), run("writer", 4))

    analyst, writer = asyncio.run(main())
    assert analyst["calls"] == 1 and analyst["tokens_in"] == 3
    assert writer["calls"] == 4 and writer["tokens_in"] == 12


def test_scope_survives_asyncio_to_thread(logger) -> None:
    def role_call() -> None:
        logger.log_success_event({}, _response(5, 7), None, None)

    async def main():
        with usage_scope("analyst") as scope:
            await asyncio.to_thread(role_call)
        return scope

    scope = asyncio.run(main())
    assert (scope.tokens_in, scope.tokens_out, scope.calls) == (5, 7, 1)


def test_run_in_scope_carries_the_scope_to_a_plain_thread(logger) -> None:
    with usage_scope("writer") as scope:
        thread = threading.Thread(
            target=run_in_scope,
            args=(scope, lambda: logger.log_success_event({}, _response(1, 1), None, None)),
        )
        thread.start()
        thread.join(timeout=5)

    assert scope.calls == 1


def test_multiple_calls_in_one_scope_sum(logger) -> None:
    """A structured call with one validation re-ask is two model calls."""
    with usage_scope("writer") as scope:
        logger.log_success_event({}, _response(100, 20, reasoning=6), None, None)
        logger.log_success_event({}, _response(140, 25, reasoning=4), None, None)

    data = scope.as_dict()
    assert data == {"tokens_in": 240, "tokens_out": 45,
                    "reasoning_tokens": 10, "calls": 2}


def test_install_is_idempotent(logger) -> None:
    import litellm

    assert install_usage_logger() is True
    assert install_usage_logger() is False
    installed = [cb for cb in litellm.callbacks if type(cb).__name__ == "UsageLogger"]
    assert len(installed) == 1

    with usage_scope("critic") as scope:
        installed[0].log_success_event({}, _response(2, 2), None, None)
    assert scope.calls == 1


def test_ack_and_stage_event_carry_the_numbers(tmp_path) -> None:
    from athena.agents.crew_server import run_role_request
    from athena.observability.metrics import agent_metrics
    from athena.observability.store import SQLiteEventStore

    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=store)
    hook = build_logger()

    def stub(run_id, instruction, store=None):
        with usage_scope("analyst") as scope:
            hook.log_success_event({}, _response(300, 40, reasoning=12), None, None)
            hook.log_success_event({}, _response(320, 44, reasoning=8), None, None)
        return {"section": "analysis", "version": 1, **scope.as_dict()}

    ack_text = run_role_request(
        json.dumps({"run_id": blackboard.run_id, "role": "analyst", "instruction": "go"}),
        roles={"analyst": stub},
        store=store,
    )
    ack = json.loads(ack_text)

    assert ack["tokens_in"] == 620
    assert ack["tokens_out"] == 84
    assert ack["reasoning_tokens"] == 20
    assert ack["calls"] == 2
    assert len(ack_text) < 300

    events = SQLiteEventStore().events(blackboard.run_id)
    stage_end = [event for event in events if event.kind == "stage_end"][0]
    assert stage_end.tokens_in == 620
    assert stage_end.tokens_out == 84
    assert stage_end.extra["reasoning_tokens"] == 20
    assert stage_end.extra["calls"] == 2

    # What crew_call emits for the same scope; the metrics table sums model_call.
    from athena.observability.events import Event

    call_event = Event(
        run_id=blackboard.run_id, kind="model_call", agent="analyst", framework="crewai",
        tokens_in=620, tokens_out=84, extra={"reasoning_tokens": 20, "calls": 2},
    )
    rows = agent_metrics([blackboard.run_id], {blackboard.run_id: [call_event]}, {})
    assert rows[0].tokens_in == 620
    assert rows[0].tokens_out == 84
    assert rows[0].reasoning_tokens == 20
