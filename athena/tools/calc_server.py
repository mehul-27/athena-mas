"""MCP server exposing ``calculate`` and ``forecast`` over stdio.

Run: ``python -m athena.tools.calc_server``
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # also allow `python athena/tools/calc_server.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server.fastmcp import FastMCP

from athena.tools.calc import calculate as _calculate
from athena.tools.forecast import forecast as _forecast
from athena.tools.schemas import CalcResult, ForecastResult

mcp = FastMCP("athena-calc")


@mcp.tool()
def calculate(expression: str) -> CalcResult:
    """Evaluate an arithmetic expression safely.

    Supports numbers, + - * / // % **, parentheses, and a small whitelist of
    functions such as sqrt, log, sin, cos, abs, round, min, max.

    Args:
        expression: The arithmetic expression, e.g. "17.5 + 24.5".
    """
    return _calculate(expression)


@mcp.tool()
def forecast(values: list[float], horizon: int) -> ForecastResult:
    """Forecast the next values of a numeric series.

    Args:
        values: At least three observed values, oldest first.
        horizon: How many steps ahead to forecast (1-50).
    """
    return _forecast(values, horizon)


if __name__ == "__main__":
    mcp.run(transport="stdio")
