"""Offline tests for the demo API, the page and the replay bundles."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from athena.api import replay
from athena.api.app import create_app
from athena.api.runs import RunRegistry

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "athena" / "api" / "static"
BUNDLES = ROOT / "data" / "demo" / "replay"


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    return TestClient(create_app())


def test_health_never_leaks_the_key(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-demo-secret-value")
    monkeypatch.setenv("LLM_MODEL", "openai/fake")
    monkeypatch.setenv("LLM_API_BASE", "http://gateway.local:8080/v1")

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["provider_configured"] is True
    assert body["api_host"] == "gateway.local"
    assert "sk-demo-secret-value" not in response.text
    assert body["crew_server"] == "down"


def test_health_without_a_key_says_so(client: TestClient, monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert client.get("/api/health").json()["provider_configured"] is False


def test_questions_include_demo_and_eval(client: TestClient) -> None:
    body = client.get("/api/questions").json()
    assert len(body["demo"]) >= 4
    assert body["demo"][-1]["kind"] == "numeric"


def test_invalid_pattern_is_rejected(client: TestClient) -> None:
    response = client.post("/api/runs", json={"question": "q", "pattern": "nonsense"})
    assert response.status_code == 422


def test_question_is_length_limited(client: TestClient) -> None:
    response = client.post("/api/runs", json={"question": "x" * 501, "pattern": "sequential"})
    assert response.status_code == 422


def test_second_run_while_busy_returns_409(monkeypatch) -> None:
    registry = RunRegistry(command=[__import__("sys").executable, "-c",
                                    "import time; time.sleep(2)"])
    app = create_app(registry)
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    client = TestClient(app)

    first = client.post("/api/runs", json={"question": "q", "pattern": "sequential"})
    second = client.post("/api/runs", json={"question": "q", "pattern": "sequential"})

    assert first.status_code == 200
    assert second.status_code == 409


def test_replay_index_and_bundle(client: TestClient) -> None:
    index = client.get("/api/replay").json()["bundles"]
    assert index, "expected committed replay bundles"
    name = index[0]["name"]

    bundle = client.get(f"/api/replay/{name}").json()

    assert bundle["run_id"]
    assert bundle["events"], "a bundle must carry events"
    assert client.get("/api/replay/nope").status_code == 404


def test_every_bundle_passes_the_api_schema() -> None:
    names = replay.names()
    assert len(names) >= 4, f"expected four bundles, found {names}"
    for name in names:
        bundle = replay.load(name)
        assert set(replay.REQUIRED) <= set(bundle)


def test_bundles_carry_no_secret_or_home_path() -> None:
    home = str(Path.home())
    for name in replay.names():
        raw = (BUNDLES / f"{name}.json").read_text(encoding="utf-8")
        # A real key looks like sk-<long token>; "risk-based" must not trip this.\n        assert not re.search(r"sk-[A-Za-z0-9]{16,}", raw)
        assert home not in raw, f"{name} leaks a home path"


# --- the page -------------------------------------------------------------


def test_page_has_the_required_panels() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for element_id in ("status-crew", "question", "pattern", "run-button", "architecture",
                       "timeline-body", "event-log", "blackboard-sections", "report",
                       "heatmap", "compare-body", "footer-run"):
        assert f'id="{element_id}"' in html, f"missing #{element_id}"


def test_page_has_no_external_assets() -> None:
    for name in ("index.html", "style.css", "app.js"):
        content = (STATIC / name).read_text(encoding="utf-8")
        remote = re.findall(r"(?:src|href)=[\x22\x27]https?://|url\(\s*[\x22\x27]?https?://", content)
        assert not remote, f"{name} loads a remote asset"


def test_page_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Athena" in response.text
    assert "Blackboard" in response.text


def test_snapshot_404_for_unknown_run(client: TestClient) -> None:
    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_export_bundle_shape(tmp_path, monkeypatch) -> None:
    from athena.demo import export

    monkeypatch.setenv("EVENTS_DB", str(ROOT / "data" / "events.db"))
    monkeypatch.setenv("BLACKBOARD_DB", str(ROOT / "data" / "blackboard.db"))
    bundle = export.build_bundle(replay.load(replay.names()[0])["run_id"])
    path = export.write_bundle(bundle, "tmp-check", tmp_path)

    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {"run_id", "summary", "events", "blackboard", "metrics"} <= set(data)
