"""Per-worker budgets: the loop must stop, whatever the model does."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.agents.budget import (
    DEFAULT_MAX_MODEL_CALLS,
    DEFAULT_MAX_TOOL_CALLS,
    DUPLICATE_RESULT,
    STOP_TEXT,
    Budget,
    call_key,
)


def _request(tools=("rag_search", "calculate")):
    return SimpleNamespace(config=SimpleNamespace(tools=list(tools)))


def _run_loop(budget: Budget, calls: list[tuple[str, dict]]) -> list:
    """Drive the callbacks the way ADK does; returns what the model saw."""
    seen = []
    for index in range(50):  # a model that never stops on its own
        request = _request()
        response = budget.before_model(None, request)
        seen.append((request, response))
        if response is not None:
            break
        name, args = calls[index % len(calls)]
        budget.before_tool(SimpleNamespace(name=name), args, None)
    return seen


def test_same_call_forever_stops_at_the_tool_budget() -> None:
    budget = Budget(max_tool_calls=3, max_model_calls=6)

    seen = _run_loop(budget, [("calculate", {"expression": "(33 + 67) / 2"})])

    # The same call is executed once and then suppressed as a duplicate.
    assert budget.tool_calls == 1
    assert budget.stop_reason in {"duplicate", "tool_budget"}
    assert len(seen) <= budget.max_model_calls + 1
    assert all(response is None for _, response in seen[:-1])


def test_alternating_calls_are_also_bounded() -> None:
    budget = Budget(max_tool_calls=3, max_model_calls=6)

    _run_loop(budget, [("calculate", {"expression": "67 - 33"}),
                       ("calculate", {"expression": "(33 + 67) / 2"})])

    assert budget.tool_calls == 2
    assert budget.stop_reason in {"tool_budget", "duplicate"}


def test_duplicate_is_detected_and_not_counted_as_new_work() -> None:
    budget = Budget(max_tool_calls=3)
    tool = SimpleNamespace(name="calculate")

    assert budget.before_tool(tool, {"expression": "1 + 1"}, None) is None
    result = budget.before_tool(tool, {"expression": "1 + 1"}, None)

    assert result == DUPLICATE_RESULT
    assert result["duplicate"] is True
    assert budget.stop_reason == "duplicate"
    assert budget.tool_calls == 1


def test_duplicate_ignores_key_order_and_whitespace() -> None:
    budget = Budget(max_tool_calls=5)
    tool = SimpleNamespace(name="rag_search")

    budget.before_tool(tool, {"query": "nasa se", "top_k": 3}, None)
    again = budget.before_tool(tool, {"top_k": 3, "query": "nasa  se "}, None)

    assert again == DUPLICATE_RESULT


def test_tools_are_removed_once_the_budget_is_spent() -> None:
    budget = Budget(max_tool_calls=1, max_model_calls=10)
    tool = SimpleNamespace(name="calculate")

    budget.before_tool(tool, {"expression": "1 + 1"}, None)  # spends the budget
    request = _request()
    budget.before_model(None, request)

    assert request.config.tools == [], "the model must not be offered tools again"


def test_model_budget_short_circuits_with_a_canned_answer() -> None:
    budget = Budget(max_tool_calls=5, max_model_calls=2)

    assert budget.before_model(None, _request()) is None
    assert budget.before_model(None, _request()) is None
    response = budget.before_model(None, _request())

    assert response is not None, "the third call must not reach the model"
    assert response.content.parts[0].text == STOP_TEXT
    assert budget.stop_reason == "model_budget"


def test_budgets_are_independent_between_workers() -> None:
    first = Budget(max_tool_calls=1)
    second = Budget(max_tool_calls=1)
    tool = SimpleNamespace(name="calculate")

    assert first.before_tool(tool, {"expression": "1"}, None) is None
    assert second.before_tool(tool, {"expression": "1"}, None) is None

    assert first.tool_calls == 1 and second.tool_calls == 1
    assert first.stop_reason == "" and second.stop_reason == ""


def test_limits_come_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("ATHENA_MAX_TOOL_CALLS", "2")
    monkeypatch.setenv("ATHENA_WORKER_MAX_MODEL_CALLS", "4")

    budget = Budget.from_env()

    assert (budget.max_tool_calls, budget.max_model_calls) == (2, 4)


def test_defaults_are_sane(monkeypatch) -> None:
    monkeypatch.delenv("ATHENA_MAX_TOOL_CALLS", raising=False)
    monkeypatch.delenv("ATHENA_WORKER_MAX_MODEL_CALLS", raising=False)

    budget = Budget.from_env()

    assert budget.max_tool_calls == DEFAULT_MAX_TOOL_CALLS == 3
    assert budget.max_model_calls == DEFAULT_MAX_MODEL_CALLS == 6


@pytest.mark.parametrize("name,args", [
    ("rag_search", {"query": "nasa", "top_k": 3}),
    ("calculate", {"expression": "(33 + 67) / 2"}),
])
def test_call_key_is_stable(name: str, args: dict) -> None:
    assert call_key(name, args) == call_key(name, dict(reversed(list(args.items()))))
