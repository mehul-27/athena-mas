"""Offline tests for the reasoning-effort parameter and the per-role budgets."""

from __future__ import annotations

import pytest

from athena.config import llm as llm_config


def test_reasoning_effort_is_absent_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    adk = llm_config.get_adk_model("planner")
    crew = llm_config.get_crewai_llm("writer")

    assert "reasoning_effort" not in adk._additional_args
    assert crew.reasoning_effort is None


def test_reasoning_effort_reaches_both_models(monkeypatch) -> None:
    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")
    adk = llm_config.get_adk_model("planner")
    crew = llm_config.get_crewai_llm("writer")

    assert adk._additional_args["reasoning_effort"] == "high"
    assert crew.reasoning_effort == "high"


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_every_level_is_forwarded(monkeypatch, level: str) -> None:
    monkeypatch.setenv("LLM_REASONING_EFFORT", level)
    assert llm_config.reasoning_effort() == level
    assert llm_config.get_crewai_llm("critic").reasoning_effort == level


def test_reasoning_effort_ignores_unknown_values(monkeypatch) -> None:
    monkeypatch.setenv("LLM_REASONING_EFFORT", "extreme")
    assert llm_config.reasoning_effort() == ""


def test_role_defaults_are_the_new_values(monkeypatch) -> None:
    for name in ("LLM_MAX_TOKENS_PLANNER", "LLM_MAX_TOKENS_RESEARCHER",
                 "LLM_MAX_TOKENS_ANALYST", "LLM_MAX_TOKENS_WRITER",
                 "LLM_MAX_TOKENS_CRITIC"):
        monkeypatch.delenv(name, raising=False)

    assert llm_config.max_tokens_for("planner") == 1500
    assert llm_config.max_tokens_for("researcher") == 2000
    assert llm_config.max_tokens_for("analyst") == 1500
    assert llm_config.max_tokens_for("writer") == 3000
    assert llm_config.max_tokens_for("critic") == 1500


def test_env_override_still_wins(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MAX_TOKENS_WRITER", "800")
    assert llm_config.max_tokens_for("writer") == 800

    monkeypatch.setenv("LLM_MAX_TOKENS_PLANNER", "1200")
    assert llm_config.max_tokens_for("planner") == 1200


def test_role_less_calls_use_the_global_cap(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MAX_TOKENS", "700")
    assert llm_config.max_tokens_for(None) == 700
