"""Streaming assembly and the bounded chat tool loop."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from athena.chat import service
from athena.chat import llm
from athena.chat.llm import LLMDelta, stream_completion
from athena.chat.models import ChatTarget
from athena.chat.store import ChatStore
from athena.settings import profiles as profiles_module
from athena.settings.profiles import Profile
from athena.settings.vault import MemoryStore, default_vault, set_vault


def _target() -> ChatTarget:
    return ChatTarget(
        model="openai/fake",
        api_key="key",
        api_base="",
        reasoning_effort="",
        provider="openai",
    )


def _chunk(*, content: str | None = None, tool: dict | None = None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=None)
    if tool is not None:
        delta.tool_calls = [
            SimpleNamespace(
                index=tool["index"],
                id=tool.get("id", ""),
                function=SimpleNamespace(
                    name=tool.get("name", ""), arguments=tool.get("arguments", "")
                ),
            )
        ]
    choice = SimpleNamespace(delta=delta, finish_reason="stop" if content else None)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_stream_completion_assembles_text_tools_and_usage(monkeypatch) -> None:
    import litellm

    async def fake_acompletion(**_kwargs):
        async def gen():
            yield _chunk(
                tool={"index": 0, "id": "call_1", "name": "calculate", "arguments": '{"expr'}
            )
            yield _chunk(tool={"index": 0, "arguments": 'ession":"2+2"}'})
            yield _chunk(
                usage=SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=3,
                    completion_tokens_details=None,
                )
            )
            yield _chunk(content="Done")

        return gen()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    async def collect():
        return [delta async for delta in stream_completion(_target(), [{"role": "user", "content": "hi"}])]

    deltas = asyncio.run(collect())

    assert "".join(delta.text for delta in deltas) == "Done"
    assert deltas[0].tool_name == "calculate"
    assert deltas[1].tool_arguments == 'ession":"2+2"}'
    assert any(delta.usage and delta.usage["tokens_in"] == 10 for delta in deltas)


def _wire_response(model, delta, finish_reason="stop"):
    import httpx

    chunk = {
        "id": "offline",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
    )


@pytest.mark.parametrize(
    ("model", "api_base", "effort", "thinking", "wire_effort"),
    [
        ("openai/deepseek-flash", "https://api.deepseek.com", "", {"type": "disabled"}, None),
        ("openai/deepseek-flash", "https://api.deepseek.com", "off", {"type": "disabled"}, None),
        ("openai/deepseek-flash", "https://api.deepseek.com/v1", "low", None, "low"),
        ("openai/deepseek-flash", "https://gateway.example/v1", "", None, None),
        ("groq/qwen/qwen3.8-27b", "https://api.groq.com/openai/v1", "off", None, "none"),
        ("openai/gpt-5.4", "https://api.openai.com/v1", "off", None, "none"),
    ],
)
def test_reasoning_effort_on_chat_wire(monkeypatch, model, api_base, effort, thinking, wire_effort) -> None:
    import httpx
    import litellm
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
    from openai import AsyncOpenAI

    limiter_calls = []
    limiter = SimpleNamespace(
        acquire=lambda *_: limiter_calls.append("acquire"),
        record=lambda *_: limiter_calls.append("record"),
    )
    monkeypatch.setattr(llm, "get_limiter", lambda: limiter)
    monkeypatch.setattr(llm, "get_output_limiter", lambda: limiter)
    requests = []

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        return _wire_response(body["model"], {"content": "OK"})

    completion = litellm.acompletion
    target = replace(
        _target(), model=model, api_base=api_base, reasoning_effort=effort
    )

    async def collect():
        transport = httpx.MockTransport(provider)
        if model.startswith("groq/"):
            client = AsyncHTTPHandler(transport=transport)
            http = client.client
        else:
            http = httpx.AsyncClient(transport=transport)
            client = AsyncOpenAI(api_key="offline-test", base_url=api_base, http_client=http)

        async with http:
            async def mocked_connection(**kwargs):
                return await completion(**kwargs, client=client)

            monkeypatch.setattr(litellm, "acompletion", mocked_connection)
            return [delta async for delta in stream_completion(target, [{"role": "user", "content": "hi"}])]

    assert "".join(delta.text for delta in asyncio.run(collect())) == "OK"
    (request,) = requests
    assert request.get("thinking") == thinking
    assert request.get("reasoning_effort") == wire_effort
    assert limiter_calls == (
        [] if "api.deepseek.com" in api_base else ["acquire", "acquire", "record", "record"]
    )


@pytest.fixture()
def _profile(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles_module, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(profiles_module, "ACTIVE_FILE", tmp_path / "active.json")
    monkeypatch.setattr(profiles_module, "PRESETS_FILE", tmp_path / "presets.json")
    set_vault(MemoryStore())
    profile = Profile(name="Fake", model="openai/fake", api_base="https://gateway.example/v1")
    profiles_module.upsert(profile)
    default_vault().set(profiles_module.key_name(profile.id, "llm"), "key")
    profiles_module.set_active(profile.id)
    yield profile
    set_vault(None)


def _collect(ctx):
    async def run():
        return [event async for event in service.stream_turn(ctx)]

    return asyncio.run(run())


def _done(events):
    return [payload for name, payload in events if name == "done"][-1]


def test_deepseek_thinking_is_replayed_only_to_continue_same_turn_tools(_profile, tmp_path, monkeypatch):
    import httpx
    import litellm
    from openai import AsyncOpenAI

    _profile.model = "openai/deepseek-flash"
    _profile.api_base = "https://api.deepseek.com"
    profiles_module.upsert(_profile)
    store = ChatStore(tmp_path / "chat.db")
    context = service.prepare_turn(
        store, conversation_id=None, message="What is 2+2?", profile_id=None,
        capabilities=[], tool_names=["calculate"], reasoning_effort="high",
    )
    reasoning = "Private provider scratch work, not visible chat text."
    requests = []

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return _wire_response(body["model"], {
                "reasoning_content": reasoning,
                "tool_calls": [{
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "calculate", "arguments": '{"expression":"2+2"}'},
                }],
            }, "tool_calls")
        return _wire_response(body["model"], {"content": "The answer is 4."})

    completion = litellm.acompletion

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            client = AsyncOpenAI(api_key="offline-test", base_url=_profile.api_base, http_client=http)

            async def mocked_connection(**kwargs):
                return await completion(**kwargs, client=client)

            monkeypatch.setattr(litellm, "acompletion", mocked_connection)
            return [event async for event in service.stream_turn(context)]

    events = asyncio.run(collect())
    first, continuation = requests
    assert first["reasoning_effort"] == "high"
    assert reasoning not in json.dumps(first)
    previous = next(message for message in continuation["messages"] if message["role"] == "assistant")
    assert previous["reasoning_content"] == reasoning
    assert _done(events)["message"]["content"] == "The answer is 4."
    assert reasoning not in json.dumps(events)
    assert reasoning not in json.dumps(store.messages(context.conversation_id))


def test_tool_loop_runs_the_selected_tool_then_answers(_profile, tmp_path, monkeypatch) -> None:
    calls = {"n": 0}

    async def fake_stream(target, messages, *, tools=None, max_tokens=3000):
        calls["n"] += 1
        if calls["n"] == 1:
            yield LLMDelta(
                tool_index=0,
                tool_id="call_1",
                tool_name="calculate",
                tool_arguments='{"expression":"2+2"}',
            )
        else:
            yield LLMDelta(text="The answer is 4.")

    async def fake_execute(name, args, target, timeout=30):
        assert name == "calculate"
        return "2+2 = 4", {
            "name": "calculate",
            "label": "Calculator",
            "status": "ok",
            "summary": "= 4",
            "elapsed_ms": 1.0,
            "error": "",
        }

    monkeypatch.setattr(service, "stream_completion", fake_stream)
    monkeypatch.setattr(service.tools, "execute", fake_execute)

    store = ChatStore(tmp_path / "chat.db")
    ctx = service.prepare_turn(
        store,
        conversation_id=None,
        message="What is 2+2?",
        profile_id=None,
        capabilities=[],
        tool_names=["calculate"],
    )
    events = _collect(ctx)

    tool_events = [payload for name, payload in events if name == "tool"]
    assert [event["status"] for event in tool_events] == ["running", "ok"]
    assert _done(events)["message"]["content"] == "The answer is 4."
    assert store.get_message(ctx.assistant_message["id"])["tool_events"][0]["status"] == "ok"


def test_tools_rejected_by_the_provider_fall_back(_profile, tmp_path, monkeypatch) -> None:
    async def fake_stream(target, messages, *, tools=None, max_tokens=3000):
        if tools:
            raise RuntimeError("tools are not supported")
        yield LLMDelta(text="Plain answer")

    monkeypatch.setattr(service, "stream_completion", fake_stream)

    store = ChatStore(tmp_path / "chat.db")
    ctx = service.prepare_turn(
        store,
        conversation_id=None,
        message="Hi",
        profile_id=None,
        capabilities=[],
        tool_names=["calculate"],
    )
    events = _collect(ctx)

    assert _done(events)["message"]["content"] == "Plain answer"
    assert any(
        name == "note" and service.NO_TOOL_SUPPORT in payload["text"]
        for name, payload in events
    )


def test_rounds_are_bounded_and_a_final_tool_free_call_runs(
    _profile, tmp_path, monkeypatch
) -> None:
    seen_tools: list = []

    async def fake_stream(target, messages, *, tools=None, max_tokens=3000):
        seen_tools.append(tools)
        if tools is None:
            yield LLMDelta(text="Final answer")
            return
        yield LLMDelta(
            tool_index=0,
            tool_id="call_1",
            tool_name="calculate",
            tool_arguments='{"expression":"1+1"}',
        )

    async def fake_execute(name, args, target, timeout=30):
        return "1+1 = 2", {
            "name": name,
            "label": "Calculator",
            "status": "ok",
            "summary": "= 2",
            "elapsed_ms": 1.0,
            "error": "",
        }

    monkeypatch.setattr(service, "MAX_ROUNDS", 2)
    monkeypatch.setattr(service, "MAX_TOOL_CALLS", 1)
    monkeypatch.setattr(service, "stream_completion", fake_stream)
    monkeypatch.setattr(service.tools, "execute", fake_execute)

    store = ChatStore(tmp_path / "chat.db")
    ctx = service.prepare_turn(
        store,
        conversation_id=None,
        message="Hi",
        profile_id=None,
        capabilities=[],
        tool_names=["calculate"],
    )
    events = _collect(ctx)

    assert seen_tools[-1] is None
    assert _done(events)["message"]["content"] == "Final answer"
    assert any(name == "note" for name, _ in events)


def test_usage_is_only_reported_when_present(_profile, tmp_path, monkeypatch) -> None:
    async def fake_stream(target, messages, *, tools=None, max_tokens=3000):
        yield LLMDelta(text="No usage here")

    monkeypatch.setattr(service, "stream_completion", fake_stream)

    store = ChatStore(tmp_path / "chat.db")
    ctx = service.prepare_turn(
        store,
        conversation_id=None,
        message="Hi",
        profile_id=None,
        capabilities=[],
        tool_names=[],
    )
    events = _collect(ctx)

    assert not [name for name, _ in events if name == "usage"]
    assert _done(events)["message"]["usage"] is None


def test_provider_failure_finalises_an_error(_profile, tmp_path, monkeypatch) -> None:
    async def fake_stream(target, messages, *, tools=None, max_tokens=3000):
        raise RuntimeError("provider down")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(service, "stream_completion", fake_stream)

    store = ChatStore(tmp_path / "chat.db")
    ctx = service.prepare_turn(
        store,
        conversation_id=None,
        message="Hi",
        profile_id=None,
        capabilities=[],
        tool_names=[],
    )
    events = _collect(ctx)
    errors = [payload for name, payload in events if name == "error"]
    done = _done(events)

    assert errors and "provider down" in errors[0]["detail"]
    assert done["message"]["status"] == "error"
    assert "provider down" in done["message"]["error"]
    assert store.get_message(ctx.assistant_message["id"])["status"] == "error"
