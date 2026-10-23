"""Offline tests for the safe calculator and the forecast tool."""

from __future__ import annotations

import pytest

from athena.tools.calc import calculate
from athena.tools.forecast import forecast


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2 + 3", 5),
        ("17.5 + 24.5", 42.0),
        ("2 ** 10", 1024),
        ("(1 + 2) * 3", 9),
        ("10 % 3", 1),
        ("7 // 2", 3),
        ("sqrt(16)", 4),
        ("max(3, 7, 5)", 7),
        ("round(2.567, 2)", 2.57),
        ("-3 + 1", -2),
    ],
)
def test_calculate_returns_expected_values(expression: str, expected: float) -> None:
    result = calculate(expression)
    assert result.ok, result.error
    assert result.value == pytest.approx(expected)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os')",
        "open('secrets.txt')",
        "().__class__",
        "x + 1",
        "'a string'",
        "[1, 2][0]",
        "lambda: 1",
        "a.b",
        "exec('print(1)')",
    ],
)
def test_calculate_rejects_unsafe_or_unknown_syntax(expression: str) -> None:
    result = calculate(expression)
    assert not result.ok
    assert result.error
    assert result.value is None


def test_calculate_reports_division_by_zero() -> None:
    result = calculate("1 / 0")
    assert not result.ok
    assert "division by zero" in (result.error or "").lower()


def test_calculate_carries_tool_metadata() -> None:
    result = calculate("1 + 1")
    assert result.tool_name == "calculate"
    assert result.elapsed_ms >= 0


def test_forecast_linear_trend() -> None:
    result = forecast([1, 2, 3, 4, 5], 3)
    assert result.ok, result.error
    assert result.method == "linear_trend"
    assert result.forecast == pytest.approx([6, 7, 8], abs=1e-3)
    assert result.note


def test_forecast_falls_back_to_exponential_smoothing() -> None:
    result = forecast([10, 1, 9, 2, 8, 3], 2)
    assert result.ok, result.error
    assert result.method == "exponential_smoothing"
    assert len(result.forecast) == 2


@pytest.mark.parametrize(
    ("values", "horizon"),
    [
        ([1, 2], 3),
        ([], 3),
        ([1, 2, 3], 0),
        ([1, 2, 3], 51),
        ([1, 2, 3], -1),
        ([1, float("nan"), 3], 2),
    ],
)
def test_forecast_rejects_invalid_input(values: list[float], horizon: int) -> None:
    result = forecast(values, horizon)
    assert not result.ok
    assert result.error


def test_forecast_carries_tool_metadata() -> None:
    result = forecast([1, 2, 3], 1)
    assert result.tool_name == "forecast"
    assert result.values == [1.0, 2.0, 3.0]
    assert result.horizon == 1
