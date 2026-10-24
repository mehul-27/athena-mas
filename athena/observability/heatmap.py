"""Per-agent heatmap: one row per agent, one column per metric.

Each column is normalised independently (0-1) for colour, and the raw value is
printed in the cell. The same table is written as CSV next to the PNG.
"""

from __future__ import annotations

import csv
from pathlib import Path

from athena.observability.metrics import COLUMNS, AgentRow

COLUMN_LABELS = {
    "stage_ms": "stage ms",
    "model_calls": "model calls",
    "tokens_in": "tokens in",
    "tokens_out": "tokens out",
    "tool_calls": "tool calls",
    "errors": "errors",
    "limiter_wait_ms": "limiter wait ms",
    "a2a_bytes": "a2a bytes",
    "evidence_items": "evidence",
}


def _normalise(values: list[float]) -> list[float]:
    low, high = min(values), max(values)
    if high <= low:
        return [0.0 if high == 0 else 0.5 for _ in values]
    return [(value - low) / (high - low) for value in values]


def write_csv(rows: list[AgentRow], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["agent", "framework", *COLUMNS, "runs"])
        for row in rows:
            values = row.as_dict()
            writer.writerow([row.agent, row.framework, *[values[name] for name in COLUMNS], row.runs])
    return path


def render_heatmap(rows: list[AgentRow], path: str | Path, title: str) -> Path:
    """Write ``path`` (PNG) plus a CSV beside it. Returns the PNG path."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows, target.with_suffix(".csv"))

    agents = [row.agent for row in rows] or ["(none)"]
    matrix = []
    for column in COLUMNS:
        raw = [float(row.as_dict()[column]) for row in rows] or [0.0]
        matrix.append((column, raw, _normalise(raw)))

    figure, axis = plt.subplots(figsize=(1.35 * len(COLUMNS) + 2, 0.55 * len(agents) + 1.4))
    data = [[matrix[col][2][row] for col in range(len(COLUMNS))] for row in range(len(rows))]
    image = axis.imshow(data, cmap="YlOrRd", aspect="auto", vmin=0, vmax=1)

    axis.set_xticks(range(len(COLUMNS)), [COLUMN_LABELS[c] for c in COLUMNS], rotation=35, ha="right")
    axis.set_yticks(range(len(agents)), agents)
    for row_index in range(len(rows)):
        for col_index, column in enumerate(COLUMNS):
            value = matrix[col_index][1][row_index]
            text = f"{value:.0f}" if abs(value) >= 10 else f"{value:.1f}"
            axis.text(col_index, row_index, text, ha="center", va="center", fontsize=8)
    axis.set_title(title, fontsize=10)
    figure.colorbar(image, ax=axis, shrink=0.7, label="normalised per column")
    figure.tight_layout()
    figure.savefig(target, dpi=130)
    plt.close(figure)
    return target
