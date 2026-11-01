"""Settings extensions: defaults, presets, library, MCP and model discovery."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from athena.api import library_routes
from athena.api.app import create_app
from athena.settings import defaults
from athena.settings import profiles as profiles_module
from athena.settings.profiles import Profile
from athena.settings.vault import MemoryStore, default_vault, set_vault


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles_module, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(profiles_module, "ACTIVE_FILE", tmp_path / "active.json")
    monkeypatch.setattr(profiles_module, "PRESETS_FILE", tmp_path / "presets.json")
    monkeypatch.setattr(defaults, "DEFAULTS_FILE", tmp_path / "defaults.json")
    set_vault(MemoryStore())
    library_routes._JOB = {
        "state": "idle",
        "started_at": "",
        "finished_at": "",
        "exit_code": None,
        "tail": "",
    }
    yield
    set_vault(None)


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    return TestClient(create_app())


def _profile(name: str = "Fake") -> Profile:
    profile = Profile(name=name, model="openai/fake", api_base="https://gateway.example/v1")
    profiles_module.upsert(profile)
    default_vault().set(profiles_module.key_name(profile.id, "llm"), "key")
    profiles_module.set_active(profile.id)
    return profile


def test_defaults_round_trip(client: TestClient) -> None:
    profile = _profile()

    assert client.get("/api/settings/defaults").json()["chat_profile_id"] == ""
    saved = client.put("/api/settings/defaults", json={"chat_profile_id": profile.id})
    assert saved.status_code == 200
    assert client.get("/api/settings/defaults").json()["chat_profile_id"] == profile.id

    env = client.put("/api/settings/defaults", json={"chat_profile_id": "__env__"})
    assert env.status_code == 200
    assert defaults.chat_profile_id() == "__env__"

    missing = client.put("/api/settings/defaults", json={"chat_profile_id": "nope"})
    assert missing.status_code == 404


def test_presets_fallback_when_the_file_is_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(profiles_module, "PRESETS_FILE", tmp_path / "missing.json")
    presets = profiles_module.presets()
    assert presets
    assert presets[0]["id"] == "commandcode"


def test_presets_fallback_when_the_file_is_invalid(monkeypatch, tmp_path) -> None:
    path = tmp_path / "presets.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(profiles_module, "PRESETS_FILE", path)
    assert profiles_module.presets()[0]["id"] == "commandcode"


def test_library_status_shape(client: TestClient) -> None:
    body = client.get("/api/settings/library").json()
    assert set(body) == {"rag", "search", "reindex"}
    assert set(body["rag"]) >= {
        "corpus_dir",
        "index_dir",
        "collection",
        "embedding_provider",
        "chunks",
        "index_exists",
        "corpus_files",
    }
    assert set(body["search"]) >= {"provider", "endpoint", "note"}
    assert body["reindex"]["state"] in {"idle", "running", "done", "failed"}


def test_reindex_is_single_flight(client: TestClient, monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow() -> None:
        started.set()
        release.wait(2)

    monkeypatch.setattr(library_routes, "_run_reindex", slow)

    first = client.post("/api/settings/library/reindex")
    assert first.status_code == 200
    assert started.wait(1)

    second = client.post("/api/settings/library/reindex")
    assert second.status_code == 409

    release.set()


def test_mcp_status_lists_the_built_in_servers(client: TestClient) -> None:
    body = client.get("/api/settings/mcp").json()
    assert {server["id"] for server in body["servers"]} == {
        "athena-rag",
        "athena-web",
        "athena-calc",
    }
    assert "arbitrary" in body["note"]
    assert body["mcp_available"] is True


def test_model_discovery_against_a_local_endpoint(client: TestClient) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            payload = json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}, {"id": "model-a"}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload.encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/v1"
        body = client.post(
            "/api/settings/models", json={"api_base": base, "llm_key": "secret-key"}
        ).json()
        assert body["models"] == ["model-a", "model-b"]
        assert body["supported"] is True
        assert "secret-key" not in json.dumps(body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
