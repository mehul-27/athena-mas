"""Chat API: streaming, persistence, capability/tool policy and errors."""

from __future__ import annotations

import asyncio
import json
from typing import Iterable

import pytest
from fastapi.testclient import TestClient

from athena.api.app import create_app
from athena.chat import service
from athena.chat.llm import LLMDelta
from athena.chat.models import Grounded
from athena.chat.store import ChatStore
from athena.settings import profiles as profiles_module
from athena.settings.profiles import Profile
from athena.settings.vault import MemoryStore, default_vault, set_vault

SENTINEL = "sk-live-chat-SENTINEL-9876543210"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles_module, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(profiles_module, "ACTIVE_FILE", tmp_path / "active.json")
    monkeypatch.setattr(profiles_module, "PRESETS_FILE", tmp_path / "presets.json")
    (tmp_path / "presets.json").write_text("[]", encoding="utf-8")
    set_vault(MemoryStore())
    yield
    set_vault(None)


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    return TestClient(create_app())


@pytest.fixture()
def profile() -> Profile:
    profile = Profile(
        name="Fake",
        model="openai/fake-model",
        api_base="https://gateway.example/v1",
        search_provider="duckduckgo",
    )
    profiles_module.upsert(profile)
    default_vault().set(profiles_module.key_name(profile.id, "llm"), SENTINEL)
    profiles_module.set_active(profile.id)
    return profile


def _fake_stream(text: str = "Hello", *, usage: bool = True, seen: dict | None = None):
    async def stream(target, messages, *, tools=None, max_tokens=3000):
        if seen is not None:
            seen["messages"] = messages
            seen["tools"] = tools
        yield LLMDelta(text=text)
        if usage:
            yield LLMDelta(
                usage={"tokens_in": 10, "tokens_out": 3, "reasoning_tokens": 0}
            )

    return stream


def _events(client: TestClient, payload: dict) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name = ""
    data: list[str] = []
    with client.stream("POST", "/api/chat/messages", json=payload) as response:
        assert response.status_code == 200, response.text
        for raw in response.iter_lines():
            line = raw.decode() if isinstance(raw, bytes) else raw
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
            elif line == "":
                if name:
                    events.append((name, json.loads("".join(data) or "{}")))
                    name = ""
                    data = []
    return events


def _done(events: Iterable[tuple[str, dict]]) -> dict:
    for name, payload in events:
        if name == "done":
            return payload
    raise AssertionError("no done event")


def test_send_streams_and_persists(client: TestClient, profile, monkeypatch) -> None:
    monkeypatch.setattr(service, "stream_completion", _fake_stream("Hello there"))

    events = _events(client, {"message": "Hi"})
    names = [name for name, _ in events]

    assert names[0] == "meta"
    assert "delta" in names
    assert names[-1] == "done"
    done = _done(events)
    assert done["message"]["content"] == "Hello there"
    assert done["message"]["status"] == "ok"
    assert done["conversation"]["title"] == "Hi"
    usage = [payload for name, payload in events if name == "usage"]
    assert usage and usage[0]["tokens_in"] == 10

    conversation_id = done["conversation"]["id"]
    reloaded = client.get(f"/api/chat/conversations/{conversation_id}").json()
    assert [message["role"] for message in reloaded["messages"]] == ["user", "assistant"]
    assert reloaded["messages"][1]["content"] == "Hello there"


def test_normal_chat_never_grounds_or_calls_tools(client: TestClient, profile, monkeypatch) -> None:
    async def fail_grounding(*_args, **_kwargs):
        raise AssertionError("grounding must not run when capabilities are off")

    def fail_tool(*_args, **_kwargs):
        raise AssertionError("tools must not run when none are selected")

    monkeypatch.setattr(service.grounding, "collect", fail_grounding)
    monkeypatch.setattr(service.tools, "execute", fail_tool)
    monkeypatch.setattr(service, "stream_completion", _fake_stream("plain"))

    events = _events(client, {"message": "Hello"})

    assert _done(events)["message"]["content"] == "plain"
    assert not [name for name, _ in events if name == "sources"]


def test_unknown_capability_is_422(client: TestClient, profile) -> None:
    response = client.post(
        "/api/chat/messages", json={"message": "Hi", "capabilities": ["nope"]}
    )
    assert response.status_code == 422
    assert "Unknown capability" in response.json()["detail"]


def test_unknown_tool_is_rejected_and_never_executed(client: TestClient, profile, monkeypatch) -> None:
    def fail_tool(*_args, **_kwargs):
        raise AssertionError("unknown tools must never execute")

    monkeypatch.setattr(service.tools, "execute", fail_tool)
    monkeypatch.setattr(service, "stream_completion", _fake_stream("ok"))

    events = _events(client, {"message": "Hi", "tools": ["rm_rf"]})
    done = _done(events)

    assert done["message"]["meta"]["rejected_tools"] == ["rm_rf"]
    assert any(name == "note" for name, _ in events)


def test_rag_sources_are_emitted_and_used_in_the_prompt(
    client: TestClient, profile, monkeypatch
) -> None:
    seen: dict = {}

    async def fake_grounding(*_args, **_kwargs):
        return Grounded(
            context="[1] Document: handbook.pdf, page 2\nEvidence text",
            sources=[
                {
                    "kind": "document",
                    "index": 1,
                    "document": "handbook.pdf",
                    "page": 2,
                    "score": 0.8,
                    "snippet": "Evidence text",
                }
            ],
        )

    monkeypatch.setattr(service.tools, "availability", lambda *a, **k: (True, ""))
    monkeypatch.setattr(service.grounding, "collect", fake_grounding)
    monkeypatch.setattr(service, "stream_completion", _fake_stream("Grounded answer", seen=seen))

    events = _events(client, {"message": "Hi", "capabilities": ["rag"]})

    assert [payload["sources"][0]["document"] for name, payload in events if name == "sources"] == [
        "handbook.pdf"
    ]
    assert "Evidence text" in json.dumps(seen["messages"])


def test_unavailable_capability_is_409(client: TestClient, profile, monkeypatch) -> None:
    monkeypatch.setattr(
        service.tools, "availability", lambda *a, **k: (False, "Web search is disabled.")
    )
    response = client.post(
        "/api/chat/messages", json={"message": "Hi", "capabilities": ["web_search"]}
    )
    assert response.status_code == 409
    assert "disabled" in response.json()["detail"]


def test_missing_key_is_409_without_leaking(client: TestClient, profile) -> None:
    default_vault().delete(profiles_module.key_name(profile.id, "llm"))

    response = client.post("/api/chat/messages", json={"message": "Hi"})

    assert response.status_code == 409
    assert "No API key is stored" in response.json()["detail"]
    assert SENTINEL not in response.text


def test_cross_origin_write_is_refused(client: TestClient, profile) -> None:
    response = client.post(
        "/api/chat/messages",
        json={"message": "Hi"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_stop_finalises_the_placeholder(tmp_path, profile) -> None:
    store = ChatStore(tmp_path / "chat.db")
    ctx = service.prepare_turn(
        store,
        conversation_id=None,
        message="Hi",
        profile_id=None,
        capabilities=[],
        tool_names=[],
    )
    ctx.stop_event.set()

    async def collect():
        return [event async for event in service.stream_turn(ctx)]

    events = asyncio.run(collect())
    done = _done(events)

    assert done["message"]["status"] == "stopped"
    assert store.get_message(ctx.assistant_message["id"])["status"] == "stopped"


def test_conversation_crud_and_model_selection(client: TestClient, profile) -> None:
    created = client.post("/api/chat/conversations", json={"title": "A chat"}).json()
    conversation_id = created["conversation"]["id"]

    renamed = client.patch(
        f"/api/chat/conversations/{conversation_id}", json={"title": "Renamed"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["conversation"]["title"] == "Renamed"

    selected = client.put(
        f"/api/chat/conversations/{conversation_id}/model",
        json={"profile_id": profile.id},
    )
    assert selected.status_code == 200
    assert selected.json()["conversation"]["profile_id"] == profile.id

    transcript = client.get(f"/api/chat/conversations/{conversation_id}/transcript")
    assert transcript.status_code == 200
    assert "Renamed" in transcript.text

    deleted = client.delete(f"/api/chat/conversations/{conversation_id}")
    assert deleted.json()["deleted"] is True
    assert client.get(f"/api/chat/conversations/{conversation_id}").status_code == 404
