"""SSE lifecycle regressions: missing task, reconnects, terminal failure."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient

import athena.api.app as app_module
from athena.api.app import create_app
from athena.observability.events import Event


class FakeStore:
    def __init__(self, events=None):
        self._events = events or []

    def events(self, _run_id):
        return list(self._events)


class FakeRegistry:
    def __init__(self, active=None):
        self.active = active

    def refresh(self):
        return self.active


def _active(run_id="r1", finished=True):
    return SimpleNamespace(run_id=run_id, pattern="sequential", finished=finished)


def _summary_event(failed=False, approved=True, severity="low"):
    return Event(
        run_id="r1", seq=1, kind="stage_end", agent="supervisor", framework="adk",
        payload_summary="run_summary sequential",
        extra={"failed": failed, "degraded": failed, "approved": approved,
               "severity": severity, "outcome": "failed" if failed else "approved"},
    )


def _client(monkeypatch, registry, events):
    monkeypatch.setattr(app_module, "_store", lambda: FakeStore(events))
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    return TestClient(create_app(registry))


def test_stream_missing_task_after_accept_terminates_without_asgi_exception(
    monkeypatch,
) -> None:
    client = _client(monkeypatch, FakeRegistry(_active(finished=True)), [])

    response = client.get("/api/runs/r1/stream")

    assert response.status_code == 200
    assert "event: done" in response.text
    payload = json.loads(response.text.split("event: done\ndata: ", 1)[1].split("\n\n", 1)[0])
    assert payload["summary"]["failed"] is True
    assert payload["summary"]["outcome"] == "failed"
    assert "runtimeerror" not in response.text.lower()


def test_stream_without_any_registry_entry_terminates_safely(monkeypatch) -> None:
    client = _client(monkeypatch, FakeRegistry(None), [])

    response = client.get("/api/runs/missing/stream")

    assert response.status_code == 200
    assert "event: done" in response.text
    assert '"failed": true' in response.text


def test_stream_waits_for_a_starting_run_then_completes(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "STREAM_POLL_S", 0.01)
    monkeypatch.setattr(app_module, "STREAM_MAX_S", 5)
    registry = FakeRegistry(_active(finished=False))
    store = FakeStore([_summary_event()])
    monkeypatch.setattr(app_module, "_store", lambda: store)
    calls = {"count": 0}

    def snapshot(_run_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise HTTPException(status_code=404, detail="no run")
        return {"history": []}

    monkeypatch.setattr(app_module, "_snapshot", snapshot)
    monkeypatch.setattr(app_module, "_summary", lambda _run_id: {"approved": True})

    async def collect():
        return [part async for part in app_module._events("r1", registry)]

    parts = asyncio.run(collect())
    body = "".join(parts)

    assert calls["count"] >= 2
    assert "event: done" in body
    assert '"approved": true' in body


def test_stream_reconnects_to_a_completed_run_without_false_failure(monkeypatch) -> None:
    client = _client(monkeypatch, FakeRegistry(_active(finished=True)), [_summary_event()])
    monkeypatch.setattr(app_module, "_snapshot", lambda _run_id: {"history": []})
    monkeypatch.setattr(app_module, "_summary", lambda _run_id: {"approved": True})

    first = client.get("/api/runs/r1/stream")
    second = client.get("/api/runs/r1/stream")

    assert first.status_code == second.status_code == 200
    assert "event: done" in first.text and "event: done" in second.text
    assert '"approved": true' in first.text
    assert '"approved": true' in second.text


def test_stream_preserves_a_failed_terminal_summary(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        FakeRegistry(_active(finished=True)),
        [_summary_event(failed=True, approved=False)],
    )
    monkeypatch.setattr(app_module, "_snapshot", lambda _run_id: {"history": []})
    monkeypatch.setattr(app_module, "_summary", lambda _run_id: {"failed": True, "outcome": "failed"})

    response = client.get("/api/runs/r1/stream")

    assert response.status_code == 200
    assert "event: done" in response.text
    assert '"failed": true' in response.text
    assert '"outcome": "failed"' in response.text


def test_stream_store_failure_is_terminal_without_asgi_exception(monkeypatch) -> None:
    class BrokenStore:
        def events(self, _run_id):
            raise RuntimeError("event store unavailable")

    monkeypatch.setattr(app_module, "_store", lambda: BrokenStore())
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    client = TestClient(create_app(FakeRegistry(_active(finished=True))))

    response = client.get("/api/runs/r1/stream")

    assert response.status_code == 200
    assert "event: done" in response.text
    assert '"failed": true' in response.text
    assert "Traceback" not in response.text


def test_last_event_id_resumes_without_duplicate_events(monkeypatch) -> None:
    first = Event(
        run_id="r1", seq=1, kind="stage_start", agent="researcher",
        framework="adk",
    )
    second = Event(
        run_id="r1", seq=2, kind="stage_end", agent="supervisor",
        framework="adk", payload_summary="run_summary sequential",
        extra={"approved": True},
    )
    registry = FakeRegistry(_active(finished=True))
    monkeypatch.setattr(app_module, "_store", lambda: FakeStore([first, second]))
    monkeypatch.setattr(app_module, "_snapshot", lambda _run_id: {"history": []})
    monkeypatch.setattr(app_module, "_summary", lambda _run_id: {"approved": True})

    async def collect():
        return [part async for part in app_module._events("r1", registry, 1)]

    body = "".join(asyncio.run(collect()))

    assert "id: 2" in body
    assert "id: 1" not in body
    assert "event: done" in body


def test_registry_retains_finished_run_until_the_next_start() -> None:
    from athena.api.runs import RunRegistry

    registry = RunRegistry()
    process = SimpleNamespace(poll=lambda: 0)
    registry._active = SimpleNamespace(
        run_id="r1", pattern="sequential", finished=False, exit_code=None,
        process=process, log_path=Path(".run/test-missing.log"), lines=[],
    )

    refreshed = registry.refresh()

    assert refreshed is not None
    assert refreshed.finished is True
    assert registry.active.run_id == "r1"
