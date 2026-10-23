"""forecast: linear trend or simple exponential smoothing, numpy only.

The method is chosen per series: a straight line when the fit is convincing
(R^2 >= 0.5), otherwise exponential smoothing held flat, because extrapolating
a trend that is not there would be a fabricated claim.
"""

from __future__ import annotations

import math
import time

import numpy as np

from athena.tools.schemas import ForecastResult

MIN_VALUES = 3
MIN_HORIZON, MAX_HORIZON = 1, 50
LINEAR_FIT_THRESHOLD = 0.5
ALPHA = 0.5


def forecast(values: list[float], horizon: int) -> ForecastResult:
    """Forecast a numeric series. Never raises: failures come back as ``ok=False``."""
    started = time.perf_counter()

    try:
        series = [float(value) for value in values]
    except (TypeError, ValueError):
        series = []

    def result(**kwargs) -> ForecastResult:
        return ForecastResult(
            values=series,
            horizon=horizon if isinstance(horizon, int) else -1,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            **kwargs,
        )

    if not isinstance(horizon, int) or isinstance(horizon, bool):
        return result(ok=False, error="horizon must be an integer")
    if not MIN_HORIZON <= horizon <= MAX_HORIZON:
        return result(
            ok=False, error=f"horizon must be between {MIN_HORIZON} and {MAX_HORIZON}"
        )
    if len(series) < MIN_VALUES:
        return result(
            ok=False, error=f"need at least {MIN_VALUES} values, got {len(series)}"
        )
    if not all(math.isfinite(value) for value in series):
        return result(ok=False, error="values must all be finite numbers")

    observed = np.asarray(series, dtype=float)
    steps = np.arange(len(series), dtype=float)
    slope, intercept = np.polyfit(steps, observed, 1)

    residuals = float(np.sum((observed - (slope * steps + intercept)) ** 2))
    spread = float(np.sum((observed - observed.mean()) ** 2))
    r_squared = 1.0 if spread == 0 else max(0.0, 1.0 - residuals / spread)

    future = np.arange(len(series), len(series) + horizon, dtype=float)
    if r_squared >= LINEAR_FIT_THRESHOLD:
        method = "linear_trend"
        projected = slope * future + intercept
        note = (
            f"Chose a least-squares line over the {len(series)} points "
            f"(slope {slope:.4g} per step, R^2 {r_squared:.3f})."
        )
    else:
        method = "exponential_smoothing"
        level = series[0]
        for value in series[1:]:
            level = ALPHA * value + (1 - ALPHA) * level
        projected = np.full(horizon, level)
        note = (
            f"Trend too weak for extrapolation (R^2 {r_squared:.3f}), so this is "
            f"simple exponential smoothing (alpha {ALPHA}) held flat."
        )

    return result(
        forecast=[round(float(value), 6) for value in projected],
        method=method,
        note=note,
    )
