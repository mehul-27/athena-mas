"""Activation and test-connection: the key reaches the child env and nowhere else."""

from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from athena.api import activation
from athena.api.app import create_app
from athena.settings import profiles as store
from athena.settings.vault import MemoryStore, default_vault, set_vault

SENTINEL = "sk-ACTIVATION-SENTINEL-0123456789abcdef"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(store, "ACTIVE_FILE", tmp_path / "active.json")
    monkeypatch.setattr(store, "PRESETS_FILE", tmp_path / "presets.json")
    (tmp_path / "presets.json").write_text("[]", encoding="utf-8")
    set_vault(MemoryStore())
    yield
    set_vault(None)


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    return TestClient(create_app())


def _profile(with_key: bool = True) -> dict:
    profile = store.upsert(store.Profile(
        name="Gateway", api_base="https://gateway.example/v1",
        model="openai/fake-model", reasoning_effort="low",
    ))
    if with_key:
        default_vault().set(store.key_name(profile.id, "llm"), SENTINEL)
    return profile.public()


def test_activation_starts_the_server_with_the_profile_env(monkeypatch, client) -> None:
    profile = _profile()
    seen: dict = {}
    server = {"up": True}

    def fake_stop():
        seen["stopped"] = True
        server["up"] = False

    def fake_up(**_kwargs):
        return server["up"]

    monkeypatch.setattr(activation, "stop_server", fake_stop)
    monkeypatch.setattr(activation, "server_up", fake_up)

    def fake_start(env_extra, wait_s=60):
        seen["env"] = dict(env_extra)
        server["up"] = True
        return True

    monkeypatch.setattr(activation, "start_server", fake_start)

    response = client.post("/api/settings/activate", json={"profile_id": profile["id"]})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["crew_server"] == "up"
    assert seen["stopped"] is True
    assert seen["env"]["LLM_API_KEY"] == SENTINEL, "the key must reach the child env"
    assert seen["env"]["LLM_MODEL"] == "openai/fake-model"
    assert SENTINEL not in response.text, "never echo the key"
    assert store.active_id() == profile["id"]


def test_activation_refuses_when_the_old_server_survives(monkeypatch, client) -> None:
    profile = _profile()
    started: list = []
    monkeypatch.setattr(activation, "stop_server", lambda: None)
    monkeypatch.setattr(activation, "server_up", lambda **_kwargs: True)
    monkeypatch.setattr(
        activation, "start_server",
        lambda *a, **k: started.append(1) or True,
    )

    response = client.post("/api/settings/activate", json={"profile_id": profile["id"]})

    assert response.status_code == 409
    assert "did not stop" in response.json()["detail"]
    assert started == [], "must not start over a server that never stopped"
    assert store.active_id() == "", "must not claim activation"


def test_activation_without_a_key_is_409_and_starts_nothing(monkeypatch, client) -> None:
    profile = _profile(with_key=False)
    started: list = []
    monkeypatch.setattr(activation, "start_server", lambda *a, **k: started.append(1) or True)
    monkeypatch.setattr(activation, "stop_server", lambda: started.append("stop"))

    response = client.post("/api/settings/activate", json={"profile_id": profile["id"]})

    assert response.status_code == 409
    assert "key" in response.json()["detail"].lower()
    assert started == [], "nothing may be started without a key"


def test_health_reports_the_active_profile(monkeypatch, client) -> None:
    profile = _profile()
    store.set_active(profile["id"])
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    health = client.get("/api/health").json()

    assert health["provider_configured"] is True, "a stored profile key counts"
    assert health["active_profile"]["name"] == "Gateway"
    assert health["active_profile"]["model"] == "openai/fake-model"
    assert health["active_profile"]["api_host"] == "gateway.example"
    assert health["model"] == "openai/fake-model"
    assert health["api_host"] == "gateway.example"
    assert SENTINEL not in json.dumps(health)


def test_run_subprocess_gets_the_active_profile_env(monkeypatch) -> None:
    from athena.api.runs import RunRegistry

    profile = _profile()
    store.set_active(profile["id"])
    captured: dict = {}

    class FakeProcess:
        def poll(self):
            return 0

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setenv("LLM_API_KEY", "stale-shell-key")
    monkeypatch.setattr("athena.api.runs.RUN_DIR", __import__("pathlib").Path(".run/test-runs"))

    registry = RunRegistry(command=["python", "-c", "pass"])
    registry.start("q", "sequential")

    assert captured["env"]["LLM_API_KEY"] == SENTINEL, "the profile wins over the shell"
    assert captured["env"]["LLM_MODEL"] == "openai/fake-model"


def test_no_active_profile_keeps_the_environment(monkeypatch) -> None:
    from athena.api.runs import RunRegistry

    captured: dict = {}

    class FakeProcess:
        def poll(self):
            return 0

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setenv("LLM_API_KEY", "shell-key")
    monkeypatch.setattr("athena.api.runs.RUN_DIR", __import__("pathlib").Path(".run/test-runs"))

    RunRegistry(command=["python", "-c", "pass"]).start("q", "p2p")

    assert captured["env"]["LLM_API_KEY"] == "shell-key"


def test_test_connection_returns_the_preflight_json(monkeypatch, client) -> None:
    from athena.api import probe

    profile = _profile()
    rows = [{"path": "adk-lite-llm", "ok": True, "host": "gateway.example",
             "model": "openai/fake-model", "tokens_in": 5, "tokens_out": 2,
             "reasoning": 0, "calls": 1, "error_type": "", "error": ""}]

    async def fake(_env, timeout=120):
        return rows

    monkeypatch.setattr(probe, "run_preflight", fake)

    response = client.post(f"/api/settings/profiles/{profile['id']}/test")

    assert response.status_code == 200
    assert response.json()["results"] == rows
    assert SENTINEL not in response.text

def test_test_connection_handles_a_timeout(monkeypatch, client) -> None:
    from athena.api import probe

    profile = _profile()

    async def fake(_env, timeout=120):
        return [{"path": "preflight", "ok": False, "error_type": "TimeoutExpired",
                 "error": "preflight did not finish within 120s"}]

    monkeypatch.setattr(probe, "run_preflight", fake)

    results = client.post(f"/api/settings/profiles/{profile['id']}/test").json()["results"]

    assert results[0]["ok"] is False
    assert results[0]["error_type"] == "TimeoutExpired"

def test_test_connection_handles_bad_json(monkeypatch, client) -> None:
    from athena.api import probe

    profile = _profile()

    async def fake(_env, timeout=120):
        return [{"path": "preflight", "ok": False, "error_type": "InvalidJson",
                 "error": "not json"}]

    monkeypatch.setattr(probe, "run_preflight", fake)

    results = client.post(f"/api/settings/profiles/{profile['id']}/test").json()["results"]

    assert results[0]["error_type"] == "InvalidJson"

def test_unknown_profile_is_404(client) -> None:
    assert client.post("/api/settings/profiles/nope/test").status_code == 404
