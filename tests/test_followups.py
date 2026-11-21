from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from athena.api import personal_routes
from athena.api.app import create_app
from athena.blackboard import Blackboard, FinalReport, Task


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATHENA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BLACKBOARD_DB", str(tmp_path / "blackboard.db"))
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events.db"))
    return TestClient(create_app())


def test_reminder_can_be_completed_and_reopened(client):
    params = {"title": "Prepare seminar", "due_at": "2020-01-01T09:00:00+05:30"}
    response = client.post("/api/personal/reminders", json=params)
    assert response.status_code == 201
    reminder = response.json()["reminder"]
    path = f"/api/personal/reminders/{reminder['id']}"
    assert reminder["due_at"] == "2020-01-01T03:30:00+00:00"
    assert reminder["overdue"]
    assert client.patch(path, json={"completed": None}).status_code == 422
    assert not client.patch(path, json={"completed": True}).json()["reminder"]["overdue"]
    assert client.patch(path, json={"completed": False}).json()["reminder"]["overdue"]
    assert client.get("/api/personal/reminders").json()["reminders"][0]["id"] == reminder["id"]
    assert client.delete(path).status_code == 200


def test_watchlist_detects_changes_without_realerting_on_result_rotation(client, monkeypatch):
    old = SimpleNamespace(url="https://example.com/old", title="Original", snippet="First version")
    updated = SimpleNamespace(url=old.url, title=old.title, snippet="New version")
    new = SimpleNamespace(url="https://example.com/new", title="New source", snippet="")
    searches = iter([[old], [updated, new], [new], [new, updated]])
    monkeypatch.setattr("athena.chat.target.search_settings", lambda *_: ("duckduckgo", ""))
    monkeypatch.setattr(
        "athena.tools.web.search.web_search",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, results=next(searches)),
    )
    response = client.post("/api/personal/watchlists", json={"query": "Model updates"})
    assert response.status_code == 201
    path = f"/api/personal/watchlists/{response.json()['watchlist']['id']}"
    assert client.post(f"{path}/check").json()["check"]["changes"] == []
    changes = client.post(f"{path}/check").json()["check"]["changes"]
    assert [(change["kind"], change["url"]) for change in changes] == [
        ("updated", old.url), ("added", new.url),
    ]
    assert client.post(f"{path}/check").json()["check"]["changes"] == []
    assert client.post(f"{path}/check").json()["check"]["changes"] == []
    assert len(client.get(f"{path}/changes").json()["changes"]) == 2


def test_actions_export_only_after_approval(client):
    payload = {"kind": "email_draft", "title": "Seminar", "content": "Draft text."}
    response = client.post("/api/actions", json=payload)
    assert response.status_code == 200
    path = f"/api/actions/{response.json()['action']['id']}"
    assert client.get(f"{path}/export").status_code == 409
    assert client.post(f"{path}/approve").json()["action"]["status"] == "approved"
    assert client.post(f"{path}/approve").json()["action"]["status"] == "approved"
    assert client.post(f"{path}/cancel").status_code == 409
    assert "Draft text." in client.get(f"{path}/export").text
    calendar = dict(
        payload, kind="calendar_event", start_at="2030-05-01T10:00:00",
        end_at="2030-05-01T11:00:00+05:30",
    )
    assert client.post("/api/actions", json=calendar).status_code == 422
    calendar["start_at"] += "+05:30"
    response = client.post("/api/actions", json=calendar)
    assert response.status_code == 200
    path = f"/api/actions/{response.json()['action']['id']}"
    assert client.post(f"{path}/approve").status_code == 200
    assert "DTSTART:20300501T043000Z" in client.get(f"{path}/export").text


def test_scheduled_watch_failure_is_retried_later(client, monkeypatch):
    monkeypatch.setattr("athena.chat.target.search_settings", lambda *_: ("duckduckgo", ""))
    monkeypatch.setattr(
        "athena.tools.web.search.web_search",
        lambda query, *_args, **_kwargs: SimpleNamespace(
            ok=query != "Failing watch", error="Search unavailable", results=[],
        ),
    )
    failed = client.post("/api/personal/watchlists", json={"query": "Failing watch"}).json()["watchlist"]
    working = client.post("/api/personal/watchlists", json={"query": "Working watch"}).json()["watchlist"]

    personal_routes._check_due_watchlists()

    items = {item["id"]: item for item in client.get("/api/personal/watchlists").json()["watchlists"]}
    assert items[failed["id"]]["last_checked_at"] == ""
    assert failed["next_check_at"] < items[failed["id"]]["next_check_at"] < items[working["id"]]["next_check_at"]
    assert items[working["id"]]["last_checked_at"]


def test_visual_report_routes_use_saved_and_recorded_research(client):
    run = Blackboard.create(Task(question="Example report"))
    path = f"/api/runs/{run.run_id}/report"
    assert client.get(path).status_code == 404
    run.write("final_report", FinalReport(run_id=run.run_id, answer="A saved finding.", confidence=0.9), author="test")

    response = client.get(path)
    assert response.status_code == 200
    assert "A saved finding." in response.text
    assert response.headers["cache-control"] == "no-store"
    replay = client.get("/api/replay/hierarchical/report")
    assert replay.status_code == 200
    assert "Citations by source type" in replay.text
