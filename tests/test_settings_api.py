"""Settings API: keys go in, never come out; cross-site writes are refused."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from athena.api.app import create_app
from athena.settings import profiles as profiles_module
from athena.settings.vault import MemoryStore, default_vault, set_vault

SENTINEL = "sk-live-SENTINEL-never-echo-9876543210"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    files = {
        "PROFILES_FILE": tmp_path / "profiles.json",
        "ACTIVE_FILE": tmp_path / "active.json",
    }
    for name, path in files.items():
        monkeypatch.setattr(profiles_module, name, path)
    monkeypatch.setattr(profiles_module, "PRESETS_FILE", tmp_path / "presets.json")
    (tmp_path / "presets.json").write_text(
        json.dumps([{"id": "custom", "name": "Custom", "api_base": "", "model": ""}]),
        encoding="utf-8")
    set_vault(MemoryStore())
    yield
    set_vault(None)


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    return TestClient(create_app())


def _payload(**kwargs) -> dict:
    body = {"name": "Gateway", "preset": "custom",
            "api_base": "https://gateway.example/v1", "model": "openai/fake-model",
            "llm_key": SENTINEL}
    body.update(kwargs)
    return body


def test_create_profile_stores_the_key_and_never_echoes_it(client: TestClient) -> None:
    response = client.post("/api/settings/profiles", json=_payload())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_llm_key"] is True
    assert SENTINEL not in response.text
    assert default_vault().get(profiles_module.key_name(body["id"], "llm")) == SENTINEL

    listing = client.get("/api/settings/profiles")
    assert SENTINEL not in listing.text
    assert listing.json()["profiles"][0]["has_llm_key"] is True


def test_update_then_delete_removes_the_secret(client: TestClient) -> None:
    created = client.post("/api/settings/profiles", json=_payload()).json()

    updated = client.put(f"/api/settings/profiles/{created['id']}",
                         json=_payload(name="Renamed", llm_key=""))
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed"

    assert client.delete(f"/api/settings/profiles/{created['id']}").status_code == 200
    assert client.get("/api/settings/profiles").json()["profiles"] == []
    assert default_vault().has(profiles_module.key_name(created["id"], "llm")) is False


def test_invalid_url_is_rejected(client: TestClient) -> None:
    response = client.post("/api/settings/profiles",
                           json=_payload(api_base="http://gateway.example/v1"))
    assert response.status_code == 422


def test_active_profile_round_trip(client: TestClient) -> None:
    created = client.post("/api/settings/profiles", json=_payload()).json()

    assert client.put("/api/settings/active",
                      json={"profile_id": created["id"]}).status_code == 200
    assert client.get("/api/settings/active").json()["profile_id"] == created["id"]
    assert client.put("/api/settings/active", json={"profile_id": "nope"}).status_code == 404


def test_cross_origin_write_is_refused(client: TestClient) -> None:
    response = client.post("/api/settings/profiles", json=_payload(),
                           headers={"Origin": "https://evil.example"})

    assert response.status_code == 403
    assert "cross-origin" in response.json()["detail"]


def test_same_origin_write_is_allowed(client: TestClient) -> None:
    response = client.post("/api/settings/profiles", json=_payload(),
                           headers={"Origin": "http://127.0.0.1:8000"})
    assert response.status_code == 200


def test_unexpected_host_is_refused(client: TestClient) -> None:
    response = client.get("/api/settings/profiles",
                          headers={"Host": "evil.example"})
    assert response.status_code == 400


def test_presets_are_served_without_keys(client: TestClient) -> None:
    presets = client.get("/api/settings/presets").json()["presets"]
    assert presets and presets[0]["id"] == "custom"
