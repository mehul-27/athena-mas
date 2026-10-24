"""Offline tests for endpoint propagation and the runtime guard.

No network: litellm's completion entry point is stubbed, so every assertion is
about the kwargs that *would* have been sent.
"""

from __future__ import annotations

import os

import pytest

from athena.config import endpoint_guard, llm
from athena.config.endpoint_guard import EndpointMismatchError, host_of

GATEWAY = "http://gateway.local:8080/v1"
EXPECTED = "gateway.local"


@pytest.fixture(autouse=True)
def _gateway_env(monkeypatch):
    monkeypatch.setenv("LLM_API_BASE", GATEWAY)
    monkeypatch.setenv("LLM_MODEL", "openai/some-org/some-model")
    for name in ("OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_API_KEY", "ATHENA_ENDPOINT_GUARD"):
        monkeypatch.delenv(name, raising=False)
    endpoint_guard._INSTALLED = False
    yield
    endpoint_guard._INSTALLED = False


def test_export_endpoint_env_points_the_sdks_at_the_gateway(monkeypatch) -> None:
    exported = llm.export_endpoint_env()

    assert "OPENAI_API_BASE" in exported
    assert host_of(os.environ["OPENAI_BASE_URL"]) == EXPECTED
    assert host_of(os.environ["OPENAI_API_BASE"]) == EXPECTED


def test_export_does_not_overwrite(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "http://elsewhere.local/v1")
    llm.export_endpoint_env()
    assert host_of(os.environ["OPENAI_BASE_URL"]) == "elsewhere.local"


def test_crewai_plain_call_carries_api_base_and_key(monkeypatch) -> None:
    import litellm

    seen: dict = {}

    def fake(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here - we only inspect the call")

    monkeypatch.setattr(litellm, "completion", fake)
    client = llm.get_crewai_llm("analyst")
    params = client._prepare_completion_params(messages=[{"role": "user", "content": "hi"}])

    assert host_of(params["api_base"]) == EXPECTED
    assert params["api_key"] == os.environ["LLM_API_KEY"]
    endpoint_guard.check_call(params["model"], params)  # guard is satisfied


def test_structured_output_path_resolves_via_the_environment(monkeypatch) -> None:
    """InternalInstructor passes no api_base; the exported env must cover it."""
    import litellm

    seen: dict = {}

    def fake(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here - we only inspect the call")

    monkeypatch.setattr(litellm, "completion", fake)
    llm.configure_process()

    from pydantic import BaseModel

    class Probe(BaseModel):
        text: str

    client = llm.get_crewai_llm("writer")
    with pytest.raises(Exception):
        client.call("Say OK", response_model=Probe)

    assert "api_base" not in seen, "CrewAI's instructor path cannot pass api_base"
    env_base = os.environ["OPENAI_BASE_URL"]
    assert host_of(env_base) == EXPECTED
    endpoint_guard.check_call(seen.get("model", ""), seen)  # env makes it correct


def test_adk_path_carries_api_base_and_key() -> None:
    model = llm.get_adk_model("planner")
    kwargs = model._additional_args

    assert host_of(kwargs["api_base"]) == EXPECTED
    assert kwargs["api_key"] == os.environ["LLM_API_KEY"]
    endpoint_guard.check_call(model.model, kwargs)


def test_guard_raises_when_a_call_has_no_api_base(monkeypatch) -> None:
    import litellm

    reached: list = []
    monkeypatch.setattr(litellm, "completion", lambda **kw: reached.append(kw) or "ok")
    endpoint_guard.install_guard()

    with pytest.raises(EndpointMismatchError) as excinfo:
        litellm.completion(model="openai/x", messages=[{"role": "user", "content": "hi"}])

    message = str(excinfo.value)
    assert EXPECTED in message
    assert "provider default" in message
    assert os.environ["LLM_API_KEY"] not in message
    assert not reached, "the provider must never be called"


def test_guard_allows_the_configured_host(monkeypatch) -> None:
    import litellm

    monkeypatch.setattr(litellm, "completion", lambda **kw: "ok")
    endpoint_guard.install_guard()

    assert litellm.completion(model="openai/x", api_base=GATEWAY, messages=[]) == "ok"


def test_guard_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ATHENA_ENDPOINT_GUARD", "0")
    assert endpoint_guard.guard_active() is False
    assert endpoint_guard.install_guard() is False


def test_guard_is_inactive_without_a_base(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    assert endpoint_guard.guard_active() is False
