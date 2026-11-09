"""The chat compatibility retry is limited to stream_options rejection."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from athena.chat import llm
from athena.chat.models import ChatTarget
from athena.config.ratelimit import RateLimitWaitError


class ProviderError(Exception):
    def __init__(self, status_code: int, message: str, *, body=None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class _Limiter:
    def acquire(self, *_args) -> None:
        pass

    def record(self, *_args) -> None:
        pass


@pytest.fixture()
def fake_litellm(monkeypatch):
    module = SimpleNamespace(acompletion=None)
    monkeypatch.setitem(sys.modules, "litellm", module)
    monkeypatch.setattr(llm, "_GUARDED", True)
    monkeypatch.setattr(llm, "estimate_tokens", lambda _text: 8)
    monkeypatch.setattr(llm, "get_limiter", _Limiter)
    monkeypatch.setattr(llm, "get_output_limiter", _Limiter)
    return module


def _target() -> ChatTarget:
    return ChatTarget(
        model="openai/fake",
        api_key="fake",
        api_base="",
        reasoning_effort="",
        provider="openai",
    )


def _response(text: str = "ok"):
    async def chunks():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=text, tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

    return chunks()


async def _collect():
    return [
        delta
        async for delta in llm.stream_completion(
            _target(), [{"role": "user", "content": "hello"}]
        )
    ]


@pytest.mark.parametrize(
    "error",
    [
        ProviderError(
            400,
            "Unrecognized request argument supplied: stream_options",
        ),
        ProviderError(
            422,
            "invalid request",
            body={
                "error": {
                    "param": "stream_options.include_usage",
                    "code": "unsupported_parameter",
                }
            },
        ),
    ],
    ids=["message-rejection", "structured-field-rejection"],
)
def test_only_explicit_unsupported_stream_options_retries_without_field(
    fake_litellm, error: ProviderError
) -> None:
    calls: list[dict] = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise error
        return _response()

    fake_litellm.acompletion = fake_acompletion

    deltas = asyncio.run(_collect())

    assert [delta.text for delta in deltas if delta.text] == ["ok"]
    assert len(calls) == 2
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in calls[1]


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "authentication failed"),
        (429, "rate limited; stream_options unsupported"),
        (503, "service unavailable"),
        (400, "invalid request body"),
        (422, "invalid value for stream_options"),
    ],
)
def test_other_provider_errors_propagate_without_second_call(
    fake_litellm, status: int, message: str
) -> None:
    error = ProviderError(status, message)
    calls = 0

    async def fake_acompletion(**_kwargs):
        nonlocal calls
        calls += 1
        raise error

    fake_litellm.acompletion = fake_acompletion

    with pytest.raises(ProviderError) as raised:
        asyncio.run(_collect())

    assert raised.value is error
    assert calls == 1


def test_unsupported_field_error_after_stream_return_is_not_retried(
    fake_litellm,
) -> None:
    error = ProviderError(
        400,
        "Unrecognized request argument supplied: stream_options",
    )
    calls = 0

    async def fake_acompletion(**_kwargs):
        nonlocal calls
        calls += 1

        async def fail_while_iterating():
            raise error
            yield  # pragma: no cover - keep this an async generator

        return fail_while_iterating()

    fake_litellm.acompletion = fake_acompletion

    with pytest.raises(ProviderError) as raised:
        asyncio.run(_collect())

    assert raised.value is error
    assert calls == 1


@pytest.mark.parametrize("error_type", [asyncio.TimeoutError, RateLimitWaitError])
def test_local_timeout_and_rate_wait_errors_are_not_retried(
    fake_litellm, error_type
) -> None:
    error = error_type("local wait or timeout")
    calls = 0

    async def fake_acompletion(**_kwargs):
        nonlocal calls
        calls += 1
        raise error

    fake_litellm.acompletion = fake_acompletion

    with pytest.raises(error_type) as raised:
        asyncio.run(_collect())

    assert raised.value is error
    assert calls == 1


def test_cancellation_is_not_retried(fake_litellm) -> None:
    calls = 0

    async def fake_acompletion(**_kwargs):
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    fake_litellm.acompletion = fake_acompletion

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_collect())

    assert calls == 1
