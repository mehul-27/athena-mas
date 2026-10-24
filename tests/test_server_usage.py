"""Server-level usage tests: the real A2A executor, stubbed model."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from athena.blackboard import Blackboard, SQLiteStore, Task
from athena.observability.litellm_usage import usage_scope
from athena.observability.usage_hook import build_logger
from athena.observability.metrics import agent_metrics
from athena.observability.store import SQLiteEventStore


class FakeContext:
    """The parts of a2a's RequestContext the executor touches."""

    context_id = "ctx-1"
    task_id = "task-1"

    def __init__(self, text: str) -> None:
        self._text = text

    def get_user_input(self) -> str:
        return self._text


class FakeQueue:
    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


def _ack_text(event) -> str:
    part = event.parts[0]
    return getattr(part, "text", None) or part.root.text


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    monkeypatch.setenv("ATHENA_USAGE_SETTLE_S", "0")


@pytest.fixture(autouse=True)
def _restore_callbacks():
    import litellm

    before = list(getattr(litellm, "callbacks", None) or [])
    yield
    litellm.callbacks = before


def _blackboard(tmp_path) -> tuple[Blackboard, SQLiteStore]:
    store = SQLiteStore(tmp_path / "bb.db")
    return Blackboard.create(Task(question="q"), store=store), store


def test_server_startup_installs_the_logger(tmp_path) -> None:
    import litellm

    litellm.callbacks = []
    from athena.agents.crew_server import create_app

    create_app()

    assert any(type(cb).__name__ == "UsageLogger" for cb in litellm.callbacks)


def test_executor_captures_usage_from_a_worker_thread(tmp_path) -> None:
    """The server runs the role in a thread; the hook must still be counted."""
    from athena.agents.crew_server import CrewA2AExecutor

    blackboard, store = _blackboard(tmp_path)
    hook = build_logger()

    def stub_role(run_id, instruction, store=None):
        with usage_scope("analyst") as scope:
            thread = threading.Thread(
                target=hook.log_success_event,
                args=({}, SimpleNamespace(usage=SimpleNamespace(
                    prompt_tokens=250, completion_tokens=60,
                    completion_tokens_details={"reasoning_tokens": 5})), None, None),
            )
            thread.start()
            thread.join(timeout=5)
        return {"section": "analysis", "version": 1, **scope.as_dict()}

    executor = CrewA2AExecutor(roles={"analyst": stub_role}, store=store)
    queue = FakeQueue()
    request = json.dumps({"run_id": blackboard.run_id, "role": "analyst",
                          "instruction": "go"})

    asyncio.run(executor.execute(FakeContext(request), queue))

    ack = json.loads(_ack_text(queue.events[0]))
    assert ack["ok"] is True
    assert ack["tokens_in"] == 250
    assert ack["tokens_out"] == 60
    assert ack["calls"] == 1

    stage_end = [
        event for event in SQLiteEventStore().events(blackboard.run_id)
        if event.kind == "stage_end"
    ][0]
    assert (stage_end.tokens_in, stage_end.tokens_out) == (250, 60)
    assert stage_end.extra["calls"] == 1

    rows = agent_metrics([blackboard.run_id], {blackboard.run_id: [stage_end]}, {})
    assert rows[0].agent == "analyst"


def test_estimated_ack_is_marked_and_flagged() -> None:
    from athena.run_report import ack_warning

    ack = json.dumps({"ok": True, "role": "writer", "section": "final_report",
                      "version": 1, "tokens_in": 120, "tokens_out": 40,
                      "reasoning_tokens": 0, "calls": 0, "estimated": True})

    assert ack_warning(ack) == "WARNING writer usage estimated (hook did not fire)"


def test_estimated_events_mark_the_metrics_row() -> None:
    from athena.observability.events import Event

    events = [
        Event(run_id="r", kind="model_call", agent="critic", framework="crewai",
              tokens_in=90, tokens_out=30, extra={"calls": 0, "estimated": True}),
    ]
    rows = agent_metrics(["r"], {"r": events}, {})

    assert rows[0].estimated_calls == 1
    from athena.observability.metrics import format_metrics

    assert format_metrics(rows).splitlines()[-1].rstrip().endswith("*")
