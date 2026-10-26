"""Hard caps, timeouts, p2p context priority, prompt worker stop, verdict."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.agents.budget import Budget, STOP_TEXT
from athena.config import call_limits
from athena.patterns.context import P2PContext, context_budget
from athena.summary import RunSummary, summary_line


def _request():
    return SimpleNamespace(config=SimpleNamespace(tools=["calculate"]))


# --- Part 1: caps and timeouts ---------------------------------------------


def test_missing_max_tokens_is_set_to_the_role_cap() -> None:
    with call_limits.role_limits("critic", 1500):
        kwargs = call_limits.clamp({"model": "openai/x"})
    assert kwargs["max_tokens"] == 1500


def test_oversized_max_tokens_is_lowered_to_the_cap() -> None:
    with call_limits.role_limits("critic", 1500):
        kwargs = call_limits.clamp({"model": "openai/x", "max_tokens": 4000})
    assert kwargs["max_tokens"] == 1500


def test_a_smaller_request_is_left_alone() -> None:
    with call_limits.role_limits("critic", 1500):
        kwargs = call_limits.clamp({"model": "openai/x", "max_tokens": 300})
    assert kwargs["max_tokens"] == 300


def test_timeout_is_always_set(monkeypatch) -> None:
    monkeypatch.delenv("ATHENA_LLM_TIMEOUT_S", raising=False)
    with call_limits.role_limits("writer", 3000):
        kwargs = call_limits.clamp({"model": "openai/x"})
    assert kwargs["timeout"] == 120.0

    monkeypatch.setenv("ATHENA_LLM_TIMEOUT_S", "30")
    with call_limits.role_limits("writer", 3000):
        kwargs = call_limits.clamp({"model": "openai/x", "timeout": 600})
    assert kwargs["timeout"] == 30.0


def test_limits_revert_after_the_role() -> None:
    with call_limits.role_limits("critic", 1500):
        assert call_limits.current() == ("critic", 1500)
    assert call_limits.current() == ("", 0)


def test_runaway_completion_is_flagged_once() -> None:
    with call_limits.role_limits("critic", 1500):
        call_limits.note_completion(131951)
    detail = call_limits.take_runaway()

    assert detail == {"role": "critic", "tokens": 131951, "cap": 1500}
    assert call_limits.take_runaway() is None, "the flag is consumed once"


def test_normal_completion_is_not_flagged() -> None:
    with call_limits.role_limits("critic", 1500):
        call_limits.note_completion(1200)
    assert call_limits.take_runaway() is None


def test_runaway_error_type_exists() -> None:
    from athena.agents.crew_roles import RunawayOutputError

    assert issubclass(RunawayOutputError, RuntimeError)


def test_a2a_timeout_defaults_to_240_and_honours_the_env(monkeypatch) -> None:
    from athena.agents.a2a_client import a2a_timeout

    monkeypatch.delenv("ATHENA_A2A_TIMEOUT_S", raising=False)
    monkeypatch.delenv("ATHENA_A2A_TIMEOUT", raising=False)
    assert a2a_timeout() == 240.0

    monkeypatch.setenv("ATHENA_A2A_TIMEOUT_S", "90")
    assert a2a_timeout() == 90.0


# --- Part 2: p2p context priority ------------------------------------------


def _evidence(count: int, size: int = 600):
    return [
        SimpleNamespace(id=f"doc{i}", source_type="rag", source_ref=f"d{i}#p1",
                        content="x" * size)
        for i in range(count)
    ]


def test_critic_context_keeps_the_report_even_when_evidence_huge(monkeypatch) -> None:
    monkeypatch.setenv("ATHENA_P2P_CONTEXT_CHARS", "3000")
    context = P2PContext(question="q", evidence=_evidence(20), analysis="A" * 200,
                         report="ANSWER: the handbook says 67 percent. KEY POINT ONE.")

    text = context.for_role("critic")

    assert "ANSWER: the handbook says 67 percent." in text
    assert "KEY POINT ONE" in text
    assert len(text) <= 3000


def test_writer_context_keeps_the_analysis(monkeypatch) -> None:
    monkeypatch.setenv("ATHENA_P2P_CONTEXT_CHARS", "2000")
    context = P2PContext(question="q", evidence=_evidence(20),
                         analysis="ANALYSIS: three findings.")

    text = context.for_role("writer")

    assert "ANALYSIS: three findings." in text
    assert len(text) <= 2000


def test_trimming_drops_evidence_first(monkeypatch) -> None:
    monkeypatch.setenv("ATHENA_P2P_CONTEXT_CHARS", "1500")
    context = P2PContext(question="q", evidence=_evidence(20), analysis="A" * 300)

    text = context.for_role("writer")

    assert "A" * 300 in text
    assert text.count("[doc") <= 3, "evidence is what gets cut"


def test_context_budget_defaults_to_9000(monkeypatch) -> None:
    monkeypatch.delenv("ATHENA_P2P_CONTEXT_CHARS", raising=False)
    assert context_budget() == 9000


# --- Part 3: workers end promptly ------------------------------------------


def test_a_duplicate_ends_the_worker_on_the_next_model_call() -> None:
    budget = Budget(max_tool_calls=5, max_model_calls=6)
    tool = SimpleNamespace(name="calculate")

    budget.before_model(None, _request())
    assert budget.before_tool(tool, {"expression": "1 + 1"}, None) is None
    budget.before_model(None, _request())
    budget.before_tool(tool, {"expression": "1 + 1"}, None)  # duplicate

    response = budget.before_model(None, _request())

    assert response is not None, "the worker must not get another tool round"
    assert response.content.parts[0].text == STOP_TEXT
    assert budget.stop_reason == "duplicate"
    assert budget.model_calls <= 3


def test_duplicate_notice_is_directive() -> None:
    from athena.agents.budget import DUPLICATE_RESULT

    assert "Stop calling tools" in DUPLICATE_RESULT["note"]


# --- Part 4: verdict visibility --------------------------------------------


def test_summary_line_shows_the_critic_verdict() -> None:
    summary = RunSummary(pattern="p2p")
    summary.approved = True
    summary.severity = "low"
    assert "critic approved (low)" in summary_line(summary)

    summary.approved = False
    summary.severity = "high"
    assert "critic rejected (high)" in summary_line(summary)


def test_unknown_verdict_is_not_claimed() -> None:
    assert "critic unknown" in summary_line(RunSummary(pattern="sequential"))


@pytest.mark.parametrize("severity", ["low", "medium", "high"])
def test_severity_is_in_the_compare_row(severity: str) -> None:
    summary = RunSummary(pattern="p2p")
    summary.approved = False
    summary.severity = severity

    assert severity in summary.row()
