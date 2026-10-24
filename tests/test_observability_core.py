"""Offline tests for the event model, the store and the recorder."""

from __future__ import annotations

import multiprocessing
import sqlite3

import pytest

from athena.observability.events import Event
from athena.observability.recorder import Recorder, monitoring_enabled, set_recorder
from athena.observability.redact import redact
from athena.observability.store import SQLiteEventStore


def _event(run_id="run-1", kind="stage_start", agent="planner", **kwargs) -> Event:
    return Event(run_id=run_id, kind=kind, agent=agent, framework="adk", **kwargs)


def test_event_fields_default_and_validate() -> None:
    event = _event(payload_summary="hello")
    assert event.event_id and event.ts and event.seq == 0
    assert event.ok is True
    assert event.framework == "adk"


def test_event_truncates_text_fields() -> None:
    event = _event(kind="error", error_message="error " * 100, payload_summary="summary " * 100)
    assert len(event.error_message) == 300
    assert len(event.payload_summary) == 300


def test_event_redacts_a_key_in_an_error(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_testvalue0123456789abcdef")
    event = _event(kind="error", error_message="auth failed for gsk_testvalue0123456789abcdef")
    assert "gsk_testvalue" not in event.error_message
    assert "[REDACTED]" in event.error_message


def test_redact_patterns() -> None:
    assert "[REDACTED]" in redact("token sk-abcdefghijklmnop")
    assert "[REDACTED]" in redact("Authorization: Bearer abcdef1234567890")
    assert redact("nothing secret here") == "nothing secret here"


def test_extra_is_small_and_json_safe() -> None:
    event = _event(kind="a2a_call", extra={"request_bytes": 156, "big": "z " * 400, "nested": {"a": 1}})
    assert event.extra["request_bytes"] == 156
    assert len(event.extra["big"]) == 300
    assert event.extra["nested"] == {"a": 1}


def test_store_assigns_ordered_seq_per_run(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    for index in range(5):
        seq = store.emit(_event(payload_summary=f"n{index}"))
        assert seq == index + 1

    other = store.emit(_event(run_id="run-2"))
    assert other == 1
    assert [event.seq for event in store.events("run-1")] == [1, 2, 3, 4, 5]
    assert store.runs() == ["run-1", "run-2"]


def test_store_is_append_only(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    store.emit(_event())
    conn = sqlite3.connect(str(tmp_path / "events.db"))
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE events SET agent = 'mallory'")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM events")
    finally:
        conn.close()


def test_threads_and_processes_do_not_lose_events(tmp_path) -> None:
    path = str(tmp_path / "events.db")
    SQLiteEventStore(path).emit(_event())

    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_emit_many, args=(path, f"proc{index}", 10)) for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=120)

    assert [process.exitcode for process in processes] == [0, 0]
    events = SQLiteEventStore(path).events("run-1")
    assert len(events) == 21
    assert [event.seq for event in events] == list(range(1, 22))


def _emit_many(path: str, agent: str, count: int) -> None:
    store = SQLiteEventStore(path)
    for _ in range(count):
        store.emit(_event(agent=agent))


def test_recorder_span_success_and_failure(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    recorder = Recorder("run-1", store)

    with recorder.span("model_call", "planner", "adk", tokens_in=5):
        pass
    with pytest.raises(ValueError):
        with recorder.span("tool_call", "researcher", "adk"):
            raise ValueError("boom")

    events = store.events("run-1")
    assert [event.kind for event in events] == ["model_call", "error"]
    assert events[0].duration_ms is not None
    assert events[1].ok is False
    assert events[1].error_type == "ValueError"


def test_recorder_swallows_store_failures() -> None:
    class Broken:
        def emit(self, event):
            raise RuntimeError("disk gone")

    recorder = Recorder("run-1", Broken())
    recorder.emit("stage_start", "planner")

    assert recorder.failures == 1
    assert recorder._warned is True


def test_monitoring_can_be_disabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ATHENA_MONITORING", "0")
    assert monitoring_enabled() is False
    recorder = Recorder.for_run("run-1")
    recorder.emit("stage_start", "planner")
    assert recorder.enabled is False
    assert recorder.store is None


def test_set_recorder_is_reversible() -> None:
    recorder = Recorder("run-1", None, enabled=False)
    previous = set_recorder(recorder)
    assert set_recorder(previous) is recorder
