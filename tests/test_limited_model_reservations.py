from __future__ import annotations

import asyncio
import sqlite3
import sys
from types import ModuleType, SimpleNamespace

import pytest

from athena.config import limited_models
from athena.config.limited_models import RateLimitedLiteLlm
from athena.config.ratelimit import RateLimitWaitError, TokenLimiter, estimate_tokens


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT tokens, settled FROM calls ORDER BY id").fetchall()


def _drain(model, request):
    async def run():
        return [item async for item in model.generate_content_async(request)]

    return asyncio.run(run())


def _model(max_tokens: int = 24) -> RateLimitedLiteLlm:
    return RateLimitedLiteLlm(
        model="openai/fake-offline-model",
        api_key="test-only-unused",
        max_tokens=max_tokens,
        num_retries=3,
    )


def _crew_case(monkeypatch, tmp_path, output_limit=30):
    crewai_module = ModuleType("crewai")

    class FakeCrewLLM:
        def __init__(self, model, api_key=None, is_litellm=False, max_tokens=None, **kwargs):
            self.model = model
            self.api_key = api_key
            self.is_litellm = is_litellm
            self.max_tokens = max_tokens

        def call(self, **kwargs):
            raise AssertionError("test must replace provider call")

        async def acall(self, **kwargs):
            raise AssertionError("test must replace provider call")

        def _format_messages_for_provider(self, messages):
            return messages

    crewai_module.LLM = FakeCrewLLM
    llms_module = ModuleType("crewai.llms")
    cache_module = ModuleType("crewai.llms.cache")
    cache_module.CACHE_BREAKPOINT_KEY = "cache_control"
    monkeypatch.setitem(sys.modules, "crewai", crewai_module)
    monkeypatch.setitem(sys.modules, "crewai.llms", llms_module)
    monkeypatch.setitem(sys.modules, "crewai.llms.cache", cache_module)
    crew_llm = limited_models.make_crewai_llm_class()
    parent_llm = crew_llm.__mro__[1]
    clock = FakeClock()
    input_limiter = TokenLimiter(
        tmp_path / "crew-input.db", limit=1000, max_wait_s=0,
        clock=clock, sleep=clock.sleep,
    )
    output_limiter = TokenLimiter(
        tmp_path / "crew-output.db", limit=output_limit, max_wait_s=0,
        clock=clock, sleep=clock.sleep,
    )
    output_limiter.acquire(5)  # Prior output use remains if retry reservation fails.
    monkeypatch.setattr(limited_models, "get_limiter", lambda: input_limiter)
    monkeypatch.setattr(limited_models, "get_output_limiter", lambda: output_limiter)
    llm = crew_llm(
        model="openai/fake-offline-model",
        api_key="test-only-unused",
        is_litellm=True,
        max_tokens=20,
    )
    return parent_llm, llm, input_limiter, output_limiter


def test_output_wait_failure_releases_only_unsent_input(monkeypatch, tmp_path) -> None:
    clock = FakeClock()
    input_limiter = TokenLimiter(
        tmp_path / "input.db", limit=100, max_wait_s=0, clock=clock, sleep=clock.sleep
    )
    output_limiter = TokenLimiter(
        tmp_path / "output.db", limit=10, max_wait_s=0, clock=clock, sleep=clock.sleep
    )
    output_limiter.acquire(1)  # Pre-existing output use must not be refunded.
    monkeypatch.setattr(limited_models, "get_limiter", lambda: input_limiter)
    monkeypatch.setattr(limited_models, "get_output_limiter", lambda: output_limiter)
    calls = []

    async def fake_run(self, request, stream):
        calls.append((request, stream))
        raise AssertionError("output reservation failed before the provider call")

    monkeypatch.setattr(RateLimitedLiteLlm, "_run", fake_run)
    with pytest.raises(RateLimitWaitError):
        _drain(_model(max_tokens=20), "offline request")

    assert calls == []
    assert _rows(input_limiter.path) == [(0, 1)]
    assert _rows(output_limiter.path) == [(1, 0)]


def test_provider_failure_settles_conservative_estimates(monkeypatch, tmp_path) -> None:
    clock = FakeClock()
    input_limiter = TokenLimiter(
        tmp_path / "input.db", limit=1000, max_wait_s=0, clock=clock, sleep=clock.sleep
    )
    output_limiter = TokenLimiter(
        tmp_path / "output.db", limit=1000, max_wait_s=0, clock=clock, sleep=clock.sleep
    )
    monkeypatch.setattr(limited_models, "get_limiter", lambda: input_limiter)
    monkeypatch.setattr(limited_models, "get_output_limiter", lambda: output_limiter)
    calls = []

    async def fake_run(self, request, stream):
        calls.append((request, stream))
        raise ConnectionError("offline fake connection failure")

    monkeypatch.setattr(RateLimitedLiteLlm, "_run", fake_run)
    request = "offline fake request"
    with pytest.raises(ConnectionError, match="offline fake"):
        _drain(_model(), request)

    assert calls == [(request, False)]
    assert _rows(input_limiter.path) == [(estimate_tokens(request), 1)]
    assert _rows(output_limiter.path) == [(24, 1)]


def test_sync_blank_retry_wait_failure_does_not_restore_unsent_input(
    monkeypatch, tmp_path
) -> None:
    parent_llm, llm, input_limiter, output_limiter = _crew_case(monkeypatch, tmp_path)
    calls = []

    def fake_parent_call(self, **kwargs):
        calls.append(kwargs)
        return ""

    monkeypatch.setattr(parent_llm, "call", fake_parent_call)
    with pytest.raises(RateLimitWaitError):
        llm.call(messages="offline fake request")

    assert len(calls) == 1
    assert _rows(input_limiter.path) == [(estimate_tokens("offline fake request"), 1), (0, 1)]
    assert _rows(output_limiter.path) == [(5, 0), (20, 1)]


def test_async_blank_retry_wait_failure_does_not_restore_unsent_input(
    monkeypatch, tmp_path
) -> None:
    parent_llm, llm, input_limiter, output_limiter = _crew_case(monkeypatch, tmp_path)
    calls = []

    async def fake_parent_acall(self, **kwargs):
        calls.append(kwargs)
        return ""

    monkeypatch.setattr(parent_llm, "acall", fake_parent_acall)
    with pytest.raises(RateLimitWaitError):
        asyncio.run(llm.acall(messages="offline fake request"))

    assert len(calls) == 1
    assert _rows(input_limiter.path) == [(estimate_tokens("offline fake request"), 1), (0, 1)]
    assert _rows(output_limiter.path) == [(5, 0), (20, 1)]


def test_sync_blank_reply_accounts_for_both_full_request_caps(
    monkeypatch, tmp_path
) -> None:
    parent_llm, llm, input_limiter, output_limiter = _crew_case(
        monkeypatch, tmp_path, output_limit=100
    )
    replies = iter(("", "second request completed"))

    def fake_parent_call(self, **kwargs):
        return next(replies)

    monkeypatch.setattr(parent_llm, "call", fake_parent_call)
    assert llm.call(messages="offline fake request") == "second request completed"

    estimate = estimate_tokens("offline fake request")
    assert _rows(input_limiter.path) == [(estimate, 1), (estimate, 1)]
    assert _rows(output_limiter.path) == [(5, 0), (20, 1), (40, 1)]


def test_adk_blank_retry_success_without_usage_keeps_full_retry_cap(
    monkeypatch, tmp_path
) -> None:
    clock = FakeClock()
    input_limiter = TokenLimiter(
        tmp_path / "input.db", limit=1000, max_wait_s=0, clock=clock, sleep=clock.sleep
    )
    output_limiter = TokenLimiter(
        tmp_path / "output.db", limit=1000, max_wait_s=0, clock=clock, sleep=clock.sleep
    )
    monkeypatch.setattr(limited_models, "get_limiter", lambda: input_limiter)
    monkeypatch.setattr(limited_models, "get_output_limiter", lambda: output_limiter)
    calls = []

    async def fake_run(self, request, stream):
        calls.append(self._additional_args["max_tokens"])
        if len(calls) == 1:
            return [
                SimpleNamespace(
                    finish_reason="LENGTH",
                    content=SimpleNamespace(parts=[]),
                    usage_metadata=None,
                )
            ]
        return [
            SimpleNamespace(
                finish_reason="STOP",
                content=SimpleNamespace(parts=[SimpleNamespace(text="answer", thought=False)]),
                usage_metadata=None,
            )
        ]

    monkeypatch.setattr(RateLimitedLiteLlm, "_run", fake_run)
    responses = _drain(_model(max_tokens=20), "offline fake request")

    assert calls == [20, 40]
    assert len(responses) == 1
    assert _rows(output_limiter.path) == [(20, 1), (40, 1)]


def test_corrective_retry_wait_failure_is_not_reported_as_a_retry(
    monkeypatch, tmp_path
) -> None:
    rejections = []
    retries = []
    monkeypatch.setattr(limited_models.tool_call_guard, "corrective_message", lambda *_: "fix")
    monkeypatch.setattr(
        limited_models.tool_call_guard,
        "with_corrective_message",
        lambda request, correction: SimpleNamespace(model_dump_json=lambda: "second prompt"),
    )
    monkeypatch.setattr(
        limited_models.tool_call_guard,
        "record_rejection",
        lambda error, agent, will_retry, attempt: rejections.append(will_retry),
    )
    monkeypatch.setattr(limited_models, "note_retry", lambda *args, **kwargs: retries.append(args))
    monkeypatch.setattr(
        limited_models,
        "_reserve_tokens",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RateLimitWaitError("offline fake reservation refusal")
        ),
    )
    monkeypatch.setattr(
        RateLimitedLiteLlm,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("no provider request after reservation refusal")
        ),
    )

    with pytest.raises(RateLimitWaitError):
        asyncio.run(
            _model()._retry_rejected_tool_call(
                SimpleNamespace(model_dump_json=lambda: "first prompt"),
                False,
                ValueError("fake tool rejection"),
                    object(),
                    object(),
                estimate_tokens("first prompt"),
                20,
            )
        )

    assert rejections == [False]
    assert retries == []


def test_corrective_retry_empty_reply_does_not_settle_a_second_time(
    monkeypatch, tmp_path
) -> None:
    input_limiter = TokenLimiter(tmp_path / "input.db", limit=1000, max_wait_s=0)
    output_limiter = TokenLimiter(tmp_path / "output.db", limit=1000, max_wait_s=0)
    monkeypatch.setattr(limited_models, "get_limiter", lambda: input_limiter)
    monkeypatch.setattr(limited_models, "get_output_limiter", lambda: output_limiter)
    monkeypatch.setattr(
        limited_models.tool_call_guard,
        "is_tool_call_validation_error",
        lambda error: True,
    )
    monkeypatch.setattr(limited_models.tool_call_guard, "corrective_message", lambda *_: "fix")
    monkeypatch.setattr(
        limited_models.tool_call_guard,
        "with_corrective_message",
        lambda request, correction: SimpleNamespace(
            model_dump_json=lambda: "full corrected prompt"
        ),
    )
    calls = []

    async def fake_run(self, request, stream):
        calls.append(request)
        if len(calls) == 1:
            raise ValueError("fake tool-call rejection")
        if len(calls) == 2:
            return [
                SimpleNamespace(
                    finish_reason="LENGTH",
                    content=SimpleNamespace(parts=[]),
                    usage_metadata=None,
                )
            ]
        return [
            SimpleNamespace(
                finish_reason="STOP",
                content=SimpleNamespace(parts=[SimpleNamespace(text="answer", thought=False)]),
                usage_metadata=None,
            )
        ]

    monkeypatch.setattr(RateLimitedLiteLlm, "_run", fake_run)
    real_settle = limited_models._settle_estimates
    settlements = []

    def track_settle(limiter, output, input_tokens, output_tokens):
        settlements.append((input_tokens, output_tokens))
        real_settle(limiter, output, input_tokens, output_tokens)

    monkeypatch.setattr(limited_models, "_settle_estimates", track_settle)
    request = SimpleNamespace(model_dump_json=lambda: "first prompt")

    responses = _drain(_model(), request)

    assert len(calls) == 3
    assert len(settlements) == 1  # Initial rejected attempt only; helper settles retry.
    assert len(_rows(input_limiter.path)) == 3
    assert len(_rows(output_limiter.path)) == 3
    assert all(settled for _, settled in _rows(input_limiter.path))
    assert all(settled for _, settled in _rows(output_limiter.path))
    assert limited_models.has_text(responses[0])
