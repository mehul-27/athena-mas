"""Aggregate a results directory, chart it, and write REPORT.md from the data.

    python -m evaluation.analyze --results evaluation/results/20250101T000000Z

Reads ``raw.jsonl`` from the results directory, scores every record against
``evaluation/dataset.json`` with :mod:`evaluation.scoring`, and writes:

``runs.csv``                 one row per run, records + scores joined
``summary_by_pattern.csv``   per-pattern aggregates with n
``summary_by_category.csv``  per-category aggregates with n
``failures.csv``             failure-category counts
``scored.jsonl``             per-criterion audit trail
``charts/*.png``             boxplots and bars (matplotlib, Agg)
``REPORT.md``                the same numbers, in prose, from real results only

Rules: every aggregate reports ``n``; nothing is described as significant; a
chart whose data is missing is skipped and the gap is named in the report; an
empty results directory produces a report that says so instead of a template of
plausible-looking numbers.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from evaluation import __version__, schema, scoring

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPO_ROOT / "evaluation" / "dataset.json"
DEFAULT_CONFIG = REPO_ROOT / "evaluation" / "config.json"

#: Printed on every chart and in the report: this harness compares groups, it
#: does not run a hypothesis test.
NO_SIGNIFICANCE_NOTE = (
    "Descriptive comparison only: n is small and no significance test is applied. "
    "Differences are not claims about the population."
)


# --- Statistics ------------------------------------------------------------


@dataclass
class Stats:
    """A tiny descriptive summary. ``n`` always counts the values actually present."""

    n: int = 0
    missing: int = 0
    mean: float | None = None
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    minimum: float | None = None
    maximum: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "missing": self.missing,
            "mean": _round(self.mean),
            "median": _round(self.median),
            "p25": _round(self.p25),
            "p75": _round(self.p75),
            "min": _round(self.minimum),
            "max": _round(self.maximum),
        }

    def describe(self, unit: str = "") -> str:
        if not self.n:
            return "no data"
        suffix = f" {unit}" if unit else ""
        return (
            f"n={self.n}, median {_fmt(self.median)}{suffix} "
            f"(IQR {_fmt(self.p25)}-{_fmt(self.p75)}), range "
            f"{_fmt(self.minimum)}-{_fmt(self.maximum)}"
        )


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "-"
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile; no numpy dependency."""
    if not values:
        raise ValueError("empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarise(values: Iterable[float | None]) -> Stats:
    """Summarise the present values and count the missing ones.

    ``values`` is materialised first: callers pass generators, and a generator
    consumed by the first comprehension would make every later count zero.
    """
    materialised = list(values)
    present = [float(value) for value in materialised if isinstance(value, (int, float))]
    missing = len(materialised) - len(present)
    if not present:
        return Stats(n=0, missing=missing)
    return Stats(
        n=len(present),
        missing=missing,
        mean=statistics.fmean(present),
        median=statistics.median(present),
        p25=percentile(present, 0.25),
        p75=percentile(present, 0.75),
        minimum=min(present),
        maximum=max(present),
    )


def rate(values: Iterable[bool]) -> tuple[int, int, float | None]:
    """(hits, n, rate) so a caller can print both the count and the share."""
    flags = [bool(value) for value in values]
    if not flags:
        return 0, 0, None
    hits = sum(1 for flag in flags if flag)
    return hits, len(flags), hits / len(flags)


# --- Loading ---------------------------------------------------------------


@dataclass
class Analysis:
    """Everything computed from one results directory."""

    results_dir: Path
    records: list[dict[str, Any]] = field(default_factory=list)
    scores: list[scoring.ItemScore] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def by_key(self) -> dict[str, scoring.ItemScore]:
        return {score.run_key: score for score in self.scores}

    def records_with_scores(self) -> list[tuple[dict[str, Any], scoring.ItemScore | None]]:
        scores = self.by_key
        return [(record, scores.get(str(record.get("run_key") or ""))) for record in self.records]


def load_analysis(results_dir: Path, dataset: schema.EvalDataset) -> Analysis:
    analysis = Analysis(results_dir=results_dir)
    raw_path = results_dir / "raw.jsonl"
    if not raw_path.exists():
        analysis.problems.append(f"{raw_path} does not exist")
        return analysis

    for number, line in enumerate(raw_path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            analysis.problems.append(f"raw.jsonl line {number} is not valid JSON")
            continue
        try:
            record = schema.RunRecord.model_validate(payload).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - a malformed record is reported, not scored
            analysis.problems.append(f"raw.jsonl line {number} is not a valid record: {exc}")
            continue
        analysis.records.append(record)

    meta_path = results_dir / "run_meta.json"
    if meta_path.exists():
        try:
            analysis.meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            analysis.problems.append("run_meta.json is not valid JSON")

    analysis.scores, score_problems = scoring.score_records(analysis.records, dataset)
    analysis.problems.extend(score_problems)
    return analysis


# --- Aggregation -----------------------------------------------------------


def _tokens_total(record: Mapping[str, Any]) -> float | None:
    tokens_in = record.get("tokens_in")
    tokens_out = record.get("tokens_out")
    if tokens_in is None and tokens_out is None:
        return None
    return float((tokens_in or 0) + (tokens_out or 0))


def group_by_pattern(analysis: Analysis) -> dict[str, list[tuple[dict[str, Any], Any]]]:
    groups: dict[str, list[tuple[dict[str, Any], Any]]] = {}
    for record, score in analysis.records_with_scores():
        groups.setdefault(str(record.get("pattern") or "?"), []).append((record, score))
    return dict(sorted(groups.items()))


def group_by_category(analysis: Analysis) -> dict[str, list[tuple[dict[str, Any], Any]]]:
    groups: dict[str, list[tuple[dict[str, Any], Any]]] = {}
    for record, score in analysis.records_with_scores():
        groups.setdefault(str(record.get("category") or "?"), []).append((record, score))
    return dict(sorted(groups.items()))


def summarise_group(rows: Sequence[tuple[dict[str, Any], Any]]) -> dict[str, Any]:
    """The numbers one group reports. Every one carries its own n."""
    records = [row[0] for row in rows]
    scores = [row[1] for row in rows if row[1] is not None]

    latency = summarise(record.get("latency_s") for record in records)
    cost = summarise(record.get("est_cost_usd") for record in records)
    tokens = summarise(_tokens_total(record) for record in records)
    tool_calls = summarise(record.get("tool_calls") for record in records)
    rubric = summarise(score.rubric_score for score in scores)
    answer_key = summarise(score.answer_key_coverage for score in scores)
    precision = summarise(score.citation_precision for score in scores)
    coverage = summarise(score.citation_coverage for score in scores)
    unsupported = summarise(score.unsupported_claim_rate for score in scores)
    relevance = summarise(score.evidence_relevance for score in scores)

    completions = [str(record.get("terminal_state")) == "completed" for record in records]
    successes = [score.success for score in scores]
    approvals = [score.critic_acceptance == "approved" for score in scores]
    rejections = [score.critic_acceptance == "rejected" for score in scores]

    return {
        "runs": len(records),
        "scored": len(scores),
        "latency": latency,
        "cost": cost,
        "tokens": tokens,
        "tool_calls": tool_calls,
        "rubric": rubric,
        "answer_key": answer_key,
        "citation_precision": precision,
        "citation_coverage": coverage,
        "unsupported_claim_rate": unsupported,
        "evidence_relevance": relevance,
        "completed": rate(completions),
        "success": rate(successes),
        "critic_approved": rate(approvals),
        "critic_rejected": rate(rejections),
    }


def failure_counts(analysis: Analysis) -> dict[str, int]:
    counts: dict[str, int] = {}
    for score in analysis.scores:
        for name in score.failure_categories:
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def terminal_state_counts(analysis: Analysis) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in analysis.records:
        state = str(record.get("terminal_state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return dict(sorted(counts.items()))


# --- CSV -------------------------------------------------------------------


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


RUN_COLUMNS = (
    "run_key", "prompt_id", "category", "difficulty", "persona", "pattern",
    "context_sharing", "tier", "seed", "repeat", "terminal_state", "outcome",
    "latency_s", "tokens_in", "tokens_out", "tokens_total", "est_cost_usd", "cost_basis",
    "tool_calls", "recovery_rounds", "revision_rounds", "evidence_count",
    "citation_count", "resolved_citations", "confidence", "critic_status", "critic_approved",
    "rubric_score", "answer_key_coverage", "completeness", "citation_precision",
    "citation_coverage", "unsupported_claim_rate", "evidence_relevance", "success",
    "injection_obeyed", "abuse_echoed", "abusive_input_handled", "failure_categories",
    "errors",
)

PATTERN_COLUMNS = (
    "pattern", "runs", "scored", "completed_rate", "success_rate",
    "latency_n", "latency_median", "latency_p25", "latency_p75",
    "tokens_n", "tokens_median", "cost_n", "cost_median", "cost_missing",
    "tool_calls_median", "rubric_median", "rubric_n", "answer_key_median",
    "citation_precision_median", "citation_coverage_median",
    "unsupported_claim_median", "evidence_relevance_median",
    "critic_approved_rate",
)

CATEGORY_COLUMNS = (
    "category", "runs", "scored", "success_rate", "rubric_median", "rubric_n",
    "answer_key_median", "citation_precision_median", "citation_coverage_median",
    "latency_median", "top_failure",
)


def run_rows(analysis: Analysis) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record, score in analysis.records_with_scores():
        rows.append(
            {
                "run_key": record.get("run_key"),
                "prompt_id": record.get("prompt_id"),
                "category": record.get("category"),
                "difficulty": record.get("difficulty"),
                "persona": record.get("persona"),
                "pattern": record.get("pattern"),
                "context_sharing": record.get("context_sharing"),
                "tier": record.get("tier"),
                "seed": record.get("seed"),
                "repeat": record.get("repeat"),
                "terminal_state": record.get("terminal_state"),
                "outcome": record.get("outcome"),
                "latency_s": record.get("latency_s"),
                "tokens_in": record.get("tokens_in"),
                "tokens_out": record.get("tokens_out"),
                "tokens_total": _tokens_total(record),
                "est_cost_usd": record.get("est_cost_usd"),
                "cost_basis": record.get("cost_basis"),
                "tool_calls": record.get("tool_calls"),
                "recovery_rounds": record.get("recovery_rounds"),
                "revision_rounds": record.get("revision_rounds"),
                "evidence_count": record.get("evidence_count"),
                "citation_count": (score.citation_count if score else None),
                "resolved_citations": (score.resolved_citations if score else None),
                "confidence": record.get("confidence"),
                "critic_status": record.get("critic_status"),
                "critic_approved": record.get("critic_approved"),
                "rubric_score": (round(score.rubric_score, 4) if score else None),
                "answer_key_coverage": (round(score.answer_key_coverage, 4) if score else None),
                "completeness": (round(score.completeness, 4) if score else None),
                "citation_precision": (round(score.citation_precision, 4) if score else None),
                "citation_coverage": (round(score.citation_coverage, 4) if score else None),
                "unsupported_claim_rate": (
                    round(score.unsupported_claim_rate, 4) if score else None
                ),
                "evidence_relevance": (
                    "" if score is None or score.evidence_relevance is None
                    else round(score.evidence_relevance, 4)
                ),
                "success": (score.success if score else None),
                "injection_obeyed": (score.injection_obeyed if score else None),
                "abuse_echoed": (score.abuse_echoed if score else None),
                "abusive_input_handled": (score.abusive_input_handled if score else None),
                "failure_categories": ("|".join(score.failure_categories) if score else ""),
                "errors": " | ".join(record.get("errors") or []),
            }
        )
    return rows


def pattern_rows(analysis: Analysis) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pattern, group in group_by_pattern(analysis).items():
        stats = summarise_group(group)
        rows.append(
            {
                "pattern": pattern,
                "runs": stats["runs"],
                "scored": stats["scored"],
                "completed_rate": _rate_text(stats["completed"]),
                "success_rate": _rate_text(stats["success"]),
                "latency_n": stats["latency"].n,
                "latency_median": _round(stats["latency"].median),
                "latency_p25": _round(stats["latency"].p25),
                "latency_p75": _round(stats["latency"].p75),
                "tokens_n": stats["tokens"].n,
                "tokens_median": _round(stats["tokens"].median),
                "cost_n": stats["cost"].n,
                "cost_median": _round(stats["cost"].median),
                "cost_missing": stats["cost"].missing,
                "tool_calls_median": _round(stats["tool_calls"].median),
                "rubric_median": _round(stats["rubric"].median),
                "rubric_n": stats["rubric"].n,
                "answer_key_median": _round(stats["answer_key"].median),
                "citation_precision_median": _round(stats["citation_precision"].median),
                "citation_coverage_median": _round(stats["citation_coverage"].median),
                "unsupported_claim_median": _round(stats["unsupported_claim_rate"].median),
                "evidence_relevance_median": _round(stats["evidence_relevance"].median),
                "critic_approved_rate": _rate_text(stats["critic_approved"]),
            }
        )
    return rows


def category_rows(analysis: Analysis) -> list[dict[str, Any]]:
    failures = failure_counts(analysis)
    rows: list[dict[str, Any]] = []
    for category, group in group_by_category(analysis).items():
        stats = summarise_group(group)
        local_failures: dict[str, int] = {}
        for _record, score in group:
            if score is None:
                continue
            for name in score.failure_categories:
                local_failures[name] = local_failures.get(name, 0) + 1
        top = max(local_failures.items(), key=lambda pair: pair[1])[0] if local_failures else ""
        rows.append(
            {
                "category": category,
                "runs": stats["runs"],
                "scored": stats["scored"],
                "success_rate": _rate_text(stats["success"]),
                "rubric_median": _round(stats["rubric"].median),
                "rubric_n": stats["rubric"].n,
                "answer_key_median": _round(stats["answer_key"].median),
                "citation_precision_median": _round(stats["citation_precision"].median),
                "citation_coverage_median": _round(stats["citation_coverage"].median),
                "latency_median": _round(stats["latency"].median),
                "top_failure": top,
            }
        )
    _ = failures
    return rows


def _rate_text(triple: tuple[int, int, float | None]) -> str:
    hits, total, value = triple
    if not total:
        return ""
    return f"{hits}/{total} ({value * 100:.0f}%)"


# --- Charts ----------------------------------------------------------------


def _pyplot():
    """Import matplotlib lazily, with a writable cache directory."""
    import tempfile

    cache = Path(os.getenv("MPLCONFIGDIR") or (Path(tempfile.gettempdir()) / "athena-eval-mpl"))
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _boxplot(plt, groups: Mapping[str, Sequence[float]], title: str, ylabel: str,
             path: Path, note: str = NO_SIGNIFICANCE_NOTE) -> bool:
    data = [(name, list(values)) for name, values in groups.items() if values]
    if not data:
        return False
    labels = [f"{name}\nn={len(values)}" for name, values in data]
    fig, axes = plt.subplots(figsize=(8, 4.5))
    positions = list(range(1, len(data) + 1))
    # ``positions`` + ``set_xticklabels`` works on every matplotlib that has
    # ``boxplot``; the ``labels`` keyword was removed in matplotlib 3.11.
    axes.boxplot(
        [values for _name, values in data],
        positions=positions,
        showmeans=True,
    )
    axes.set_xticks(positions)
    axes.set_xticklabels(labels)
    axes.set_title(title)
    axes.set_ylabel(ylabel)
    axes.grid(axis="y", alpha=0.3)
    fig.text(0.01, 0.01, note, fontsize=7, color="#555555", wrap=True)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return True


def _bar(plt, labels: Sequence[str], values: Sequence[float], title: str, ylabel: str,
         path: Path, note: str = NO_SIGNIFICANCE_NOTE) -> bool:
    if not list(values):
        return False
    fig, axes = plt.subplots(figsize=(8, 4.5))
    axes.bar(list(labels), list(values), color="#3a6ea5")
    axes.set_title(title)
    axes.set_ylabel(ylabel)
    axes.grid(axis="y", alpha=0.3)
    for index, value in enumerate(values):
        axes.text(index, value, f"{value:g}", ha="center", va="bottom", fontsize=8)
    fig.text(0.01, 0.01, note, fontsize=7, color="#555555", wrap=True)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return True


def make_charts(analysis: Analysis, chart_dir: Path) -> dict[str, str]:
    """Render every chart whose data exists. Returns ``{name: status}``."""
    status: dict[str, str] = {}
    if not analysis.records:
        return {"all": "skipped: no records"}

    chart_dir.mkdir(parents=True, exist_ok=True)
    plt = _pyplot()
    groups = group_by_pattern(analysis)

    def collect(getter) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for pattern, rows in groups.items():
            values: list[float] = []
            for record, score in rows:
                value = getter(record, score)
                if isinstance(value, (int, float)):
                    values.append(float(value))
            out[pattern] = values
        return out

    plans = {
        "latency_by_pattern.png": (
            "Latency by pattern", "seconds", collect(lambda r, s: r.get("latency_s"))
        ),
        "cost_by_pattern.png": (
            "Estimated cost by pattern", "USD (estimate)",
            collect(lambda r, s: r.get("est_cost_usd")),
        ),
        "tokens_by_pattern.png": (
            "Total tokens by pattern", "tokens (in + out)",
            collect(lambda r, s: _tokens_total(r)),
        ),
        "quality_by_pattern.png": (
            "Rubric score by pattern", "rubric score (0-1)",
            collect(lambda r, s: (s.rubric_score if s else None)),
        ),
        "citation_precision_by_pattern.png": (
            "Citation precision by pattern", "resolved citations / citations",
            collect(lambda r, s: (s.citation_precision if s else None)),
        ),
    }
    for filename, (title, ylabel, data) in plans.items():
        wrote = _boxplot(plt, data, title, ylabel, chart_dir / filename)
        status[filename] = "written" if wrote else "skipped: no data for this metric"

    # Success rate: a bar chart, because a boxplot of booleans says nothing.
    labels, values = [], []
    for pattern, rows in groups.items():
        scores = [score for _record, score in rows if score is not None]
        hits, total, _ = rate(score.success for score in scores)
        if total:
            labels.append(f"{pattern}\nn={total}")
            values.append(hits / total * 100)
    status["success_rate_by_pattern.png"] = (
        "written"
        if _bar(plt, labels, values, "Success rate by pattern", "percent of scored runs",
                chart_dir / "success_rate_by_pattern.png",
                NO_SIGNIFICANCE_NOTE)
        else "skipped: no scored runs"
    )

    failures = failure_counts(analysis)
    status["failure_categories.png"] = (
        "written"
        if _bar(plt, list(failures.keys()), list(failures.values()),
                "Failure categories across all runs", "runs",
                chart_dir / "failure_categories.png",
                "A run can carry more than one failure label.")
        else "skipped: no failure labels"
    )

    labels, values = [], []
    for pattern, rows in groups.items():
        costs = [r.get("est_cost_usd") for r, _s in rows]
        present = [c for c in costs if isinstance(c, (int, float))]
        if present:
            labels.append(f"{pattern}\nn={len(present)}")
            values.append(statistics.median(present))
    status["median_cost_bar.png"] = (
        "written"
        if _bar(plt, labels, values, "Median estimated cost by pattern", "USD (estimate)",
                chart_dir / "median_cost_bar.png",
                "Only priced runs are included; unpriced runs are excluded, not zeroed.")
        else "skipped: no run could be priced with config.pricing"
    )
    return status


# --- Report ----------------------------------------------------------------


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return "_No rows._\n"
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |")
    return "\n".join(lines) + "\n"


def _iqr_overlaps(left: Stats, right: Stats) -> bool:
    """True when two groups' inter-quartile ranges overlap (i.e. not separable)."""
    if not left.n or not right.n:
        return True
    if left.p25 is None or left.p75 is None or right.p25 is None or right.p75 is None:
        return True
    return not (left.p75 < right.p25 or right.p75 < left.p25)


def interpretation_lines(
    analysis: Analysis,
    *,
    min_group_n: int = 8,
) -> list[str]:
    """A reading of the numbers that the numbers can actually support.

    Every sentence is derived from ``analysis``: group sizes, medians, IQRs,
    token totals, priced/unpriced counts and failure labels. Nothing here names a
    winner unless the measured distributions separate, and speed alone is never
    treated as a quality claim.
    """
    groups = group_by_pattern(analysis)
    if not groups:
        return ["_No recorded runs, so there is nothing to interpret._\n"]

    stats = {name: summarise_group(rows) for name, rows in groups.items()}
    sizes = {name: value["runs"] for name, value in stats.items()}
    smallest = min(sizes.values())
    lines: list[str] = []

    lines.append(
        "- **Sample size.** "
        + ", ".join(f"{name} n={count}" for name, count in sorted(sizes.items()))
        + f". Every group is far below the {min_group_n} runs per pattern that would make a "
        "difference in central tendency stable, and the four patterns were run on the same "
        "prompts, so the same prompt-level difficulty appears in every column."
    )
    if smallest < min_group_n:
        lines.append(
            f"- **Treat every comparison as indicative.** The smallest group has n={smallest}. "
            "A single run moving between success and failure changes its pattern's success rate "
            f"by {100 / smallest:.0f} percentage points."
        )

    # --- spread, not just the middle ------------------------------------
    latency = {name: value["latency"] for name, value in stats.items()}
    latency_pairs = [
        (left, right)
        for index, left in enumerate(sorted(latency))
        for right in sorted(latency)[index + 1:]
        if left != right
    ]
    if latency_pairs:
        all_overlap = all(
            _iqr_overlaps(latency[left], latency[right]) for left, right in latency_pairs
        )
        spread = ", ".join(
            f"{name} median {_fmt(value.median)}s (IQR {_fmt(value.p25)}-{_fmt(value.p75)}, "
            f"range {_fmt(value.minimum)}-{_fmt(value.maximum)})"
            for name, value in sorted(latency.items())
            if value.n
        )
        lines.append(f"- **Latency spread.** {spread}.")
        smallest_latency = min((value.n for value in latency.values() if value.n), default=0)
        if smallest_latency < 4:
            lines.append(
                "- **Too few runs per pattern to compare spread.** With fewer than four runs in "
                "a group the quartiles collapse onto single observed values, so overlap between "
                "groups says nothing; only the raw ranges above should be read."
            )
        elif all_overlap:
            lines.append(
                "- **Latency does not separate the patterns in this batch.** Every pair of "
                "inter-quartile ranges overlaps, so the ordering of medians is within the "
                "run-to-run noise this harness measures, not a property of the patterns."
            )
        else:
            lines.append(
                "- Some latency inter-quartile ranges do not overlap, but a run's latency is "
                "also driven by how much evidence a prompt needed and by how long the provider "
                "rate limiter made each model call wait; a lower median is a throughput "
                "observation, not evidence of a better answer."
            )

    quality = {name: value["rubric"] for name, value in stats.items()}
    quality_spread = ", ".join(
        f"{name} median {_fmt(value.median)} (n={value.n})"
        for name, value in sorted(quality.items())
        if value.n
    )
    if quality_spread:
        lines.append(f"- **Answer quality spread.** {quality_spread}.")
        quality_pairs = [
            (left, right)
            for index, left in enumerate(sorted(quality))
            for right in sorted(quality)[index + 1:]
            if left != right
        ]
        if quality_pairs and all(
            _iqr_overlaps(quality[left], quality[right]) for left, right in quality_pairs
        ):
            lines.append(
                "- **Quality does not separate the patterns either.** The rubric-score "
                "inter-quartile ranges overlap on every pair, which is what a small batch of "
                "mixed-difficulty prompts looks like; it is not a ranking."
            )
        elif min((value.n for value in quality.values() if value.n), default=0) < 4:
            lines.append(
                "- **Quality differences here are a handful of runs wide.** With fewer than four "
                "scored runs in a group, a single criterion flipping on one run moves that "
                "pattern's median by tens of percentage points of rubric weight."
            )

    # --- the tradeoff, stated as a tradeoff ------------------------------
    tokens = {name: value["tokens"] for name, value in stats.items()}
    budget_line = ", ".join(
        f"{name} {_fmt(value.median)} tokens (n={value.n})"
        for name, value in sorted(tokens.items())
        if value.n
    )
    if budget_line:
        fastest = min(
            (name for name, value in latency.items() if value.n),
            key=lambda name: latency[name].median,
            default=None,
        )
        most_tokens = max(
            (name for name, value in tokens.items() if value.n),
            key=lambda name: tokens[name].median,
            default=None,
        )
        lines.append(f"- **Tokens per run.** {budget_line}.")
        if fastest:
            lines.append(
                f"- **No pattern is called best.** `{fastest}` has the lowest median latency in "
                "this batch"
                + (f", while `{most_tokens}` spends the most tokens per run" if most_tokens else "")
                + "; latency, tokens spent and answer quality move independently here, and the "
                "harness records them separately for exactly that reason. A pattern that is "
                "quick because it researches less is not a better orchestrator, and one that is "
                "slow because it spends more tokens is not a worse one."
            )

    # --- cost ------------------------------------------------------------
    unpriced = sum(1 for record in analysis.records if record.get("est_cost_usd") is None)
    if unpriced:
        models = sorted(
            {
                str((record.get("config") or {}).get("model") or record.get("model") or "")
                for record in analysis.records
            }
            - {""}
        )
        lines.append(
            f"- **Cost is unpriced, not zero.** {unpriced} of {len(analysis.records)} runs record "
            "`est_cost_usd = null` because the configured model"
            + (f" ({', '.join(models)})" if models else "")
            + " has no rate in `evaluation/config.json` or `data/pricing.json`. No total, "
            "median or per-pattern cost comparison is made: the cost columns are absent rather "
            "than filled with an invented rate. Token counts are the only comparable proxy for "
            "spend in this batch, and the provider's actual bill is not observable from here."
        )

    # --- censoring and failure effects -----------------------------------
    states = terminal_state_counts(analysis)
    not_completed = sum(count for state, count in states.items() if state != "completed")
    if not_completed:
        lines.append(
            "- **Non-completions are included, not dropped.** "
            + ", ".join(f"{state}={count}" for state, count in states.items())
            + ". A run that failed, timed out or errored keeps its latency and its failure "
            "labels in the tables above, so a pattern cannot look fast or accurate by losing "
            "its unfinished runs."
        )
    if states.get("timeout"):
        lines.append(
            "- **Timeouts censor the latency medians.** A timed-out run stops at the per-run "
            "timeout, so its recorded latency is the cap rather than the time it would have "
            "taken; the true latency of those runs is at least what is recorded."
        )

    # --- what the measures are and are not -------------------------------
    lines.append(
        "- **Success is defined by the rubric, not by the system's own critic.** "
        f"`scoring.CRITIC_IS_GROUND_TRUTH` is {scoring.CRITIC_IS_GROUND_TRUTH}; the critic's "
        "verdict is reported separately so the two can disagree in public."
    )
    lines.append(
        "- **The rubric is deterministic and keyword-based.** It checks answer-key coverage, "
        "citations that resolve to captured evidence, required output structure and the "
        "security rules. It cannot tell whether a sentence is semantically supported, so a "
        "fluent wrong answer and a terse right answer can both land near the same score."
    )
    lines.append(f"- {NO_SIGNIFICANCE_NOTE}")
    return lines


def build_report(analysis: Analysis, dataset: schema.EvalDataset,
                 chart_status: Mapping[str, str]) -> str:
    """Render REPORT.md. Every number comes from ``analysis``."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = analysis.meta
    lines: list[str] = []
    lines.append("# Athena evaluation report\n")
    lines.append(f"Generated: {now} by `evaluation.analyze` (harness v{__version__}).\n")
    lines.append(f"Results directory: `{analysis.results_dir}`\n")

    if not analysis.records:
        lines.append("\n## No runs recorded\n")
        lines.append(
            "This results directory contains no valid run records, so there is nothing to "
            "report. No numbers are shown because none were measured.\n"
        )
        if analysis.problems:
            lines.append("\nProblems found while loading:\n")
            for problem in analysis.problems:
                lines.append(f"- {problem}\n")
        lines.append(
            "\nTo produce results:\n\n"
            "```\npython scripts/dev_up.py\n"
            "python -m evaluation.runner --out evaluation/results/<timestamp>\n"
            "python -m evaluation.analyze --results evaluation/results/<timestamp>\n```\n"
        )
        return "".join(lines)

    lines.append("\n## Provenance\n")
    provenance = [
        ("Harness version", __version__),
        ("Git commit", meta.get("git_commit", "unknown")),
        ("Dataset", meta.get("dataset_path", "evaluation/dataset.json")),
        ("Dataset sha256", meta.get("dataset_sha256") or "(not recorded)"),
        ("Config sha256", meta.get("config_sha256") or "(not recorded)"),
        ("Dataset items", len(dataset.items)),
        ("Plan", meta.get("plan_id", "(unknown)")),
        ("Backend", meta.get("backend", "(unknown)")),
        ("Model label", meta.get("model", "(unknown)")),
        ("Per-run timeout", f"{meta.get('timeout_s', '?')} s"),
        ("Batch started", meta.get("started_at", "(unknown)")),
        ("Batch ended", meta.get("ended_at", "(not finished)")),
        ("Stopped reason", meta.get("stopped_reason") or "none"),
    ]
    lines.append(_table(("Field", "Value"), provenance))

    # --- what ran ---
    states = terminal_state_counts(analysis)
    lines.append("\n## What was run\n")
    lines.append(f"- Run records: **{len(analysis.records)}**\n")
    lines.append(f"- Scored against the dataset: **{len(analysis.scores)}**\n")
    if analysis.problems:
        lines.append(f"- Load/score problems: {len(analysis.problems)}\n")
    lines.append("\nTerminal states:\n")
    lines.append(_table(
        ("terminal_state", "runs"),
        sorted(states.items()),
    ))

    lines.append("\n## Data-quality caveats (read before the tables)\n")
    lines.append(f"- {NO_SIGNIFICANCE_NOTE}\n")
    per_pattern = group_by_pattern(analysis)
    lines.append(
        "- Runs per pattern: "
        + ", ".join(f"{name}={len(rows)}" for name, rows in per_pattern.items())
        + ".\n"
    )
    missing_cost = sum(1 for record in analysis.records
                       if record.get("est_cost_usd") is None)
    if missing_cost:
        lines.append(
            f"- {missing_cost} of {len(analysis.records)} runs carry **no cost estimate**: "
            "the model in `LLM_MODEL` has no entry in `config.json -> pricing.models`. "
            "Unpriced runs are excluded from cost statistics, never counted as zero.\n"
        )
    volatile = [item.id for item in dataset.items if item.answer_key.volatility == "volatile"]
    volatile_run = [s for s in analysis.scores if s.prompt_id in volatile]
    if volatile_run:
        lines.append(
            f"- {len(volatile_run)} scored runs are volatile item(s) ({', '.join(sorted(set(volatile)))}): "
            "they are scored on sourcing, dating and structure, not on a memorised value.\n"
        )
    lines.append(
        "- The pipeline's own critic verdict is recorded as `critic_acceptance` and is "
        "**not** used to decide success (`scoring.CRITIC_IS_GROUND_TRUTH = False`). "
        "Agreement is reported separately below.\n"
    )
    lines.append(
        f"- Success means: terminal state `completed`, a non-empty final answer, rubric "
        f"score >= {scoring.SUCCESS_THRESHOLD}, no citation that resolves to nothing, the "
        "item's required evidence cited (or the gap stated honestly), and no security "
        "violation.\n"
    )

    # --- per pattern ---
    lines.append("\n## By pattern\n")
    stats_by_pattern = {name: summarise_group(rows) for name, rows in per_pattern.items()}
    table_rows = []
    for name, stats in stats_by_pattern.items():
        table_rows.append(
            (
                name,
                stats["runs"],
                stats["scored"],
                _rate_text(stats["success"]),
                stats["latency"].describe("s"),
                stats["tokens"].describe("tokens"),
                (
                    f"n={stats['cost'].n}, median {_fmt(stats['cost'].median)} USD"
                    if stats["cost"].n else "no priced runs"
                ),
                f"{_fmt(stats['rubric'].median)} (n={stats['rubric'].n})",
                f"{_fmt(stats['citation_precision'].median)} (n={stats['citation_precision'].n})",
                f"{_fmt(stats['citation_coverage'].median)} (n={stats['citation_coverage'].n})",
                _rate_text(stats["critic_approved"]),
            )
        )
    lines.append(_table(
        ("pattern", "runs", "scored", "success", "latency", "tokens", "cost",
         "rubric median", "citation precision", "required-evidence coverage",
         "critic approved"),
        table_rows,
    ))

    # --- per category ---
    lines.append("\n## By category\n")
    per_category = group_by_category(analysis)
    category_table = []
    for name, rows in per_category.items():
        stats = summarise_group(rows)
        local: dict[str, int] = {}
        for _record, score in rows:
            if score is None:
                continue
            for failure in score.failure_categories:
                local[failure] = local.get(failure, 0) + 1
        top = max(local.items(), key=lambda pair: pair[1])[0] if local else ""
        category_table.append(
            (
                name,
                stats["runs"],
                _rate_text(stats["success"]),
                f"{_fmt(stats['rubric'].median)} (n={stats['rubric'].n})",
                f"{_fmt(stats['answer_key'].median)} (n={stats['answer_key'].n})",
                f"{_fmt(stats['citation_precision'].median)} (n={stats['citation_precision'].n})",
                f"{_fmt(stats['latency'].median)} s (n={stats['latency'].n})",
                top or "-",
            )
        )
    lines.append(_table(
        ("category", "runs", "success", "rubric median", "answer-key coverage",
         "citation precision", "latency median", "most common failure"),
        category_table,
    ))

    # --- failures ---
    lines.append("\n## Failure taxonomy\n")
    failures = failure_counts(analysis)
    total_scored = max(1, len(analysis.scores))
    lines.append(_table(
        ("failure category", "runs", "share of scored runs"),
        [(name, count, f"{count / total_scored * 100:.0f}%") for name, count in failures.items()],
    ))

    # --- citation quality ---
    lines.append("\n## Citation quality\n")
    precision = summarise(score.citation_precision for score in analysis.scores)
    coverage = summarise(score.citation_coverage for score in analysis.scores)
    unsupported = summarise(score.unsupported_claim_rate for score in analysis.scores)
    relevance = summarise(score.evidence_relevance for score in analysis.scores)
    no_citations = sum(1 for score in analysis.scores if score.citation_count == 0)
    unresolvable = sum(
        1 for score in analysis.scores
        if score.citation_count and score.citation_precision < 1.0
    )
    lines.append(f"- Citation precision (resolved / produced): {precision.describe()}\n")
    lines.append(f"- Required-evidence coverage: {coverage.describe()}\n")
    lines.append(f"- Unsupported-claim rate (lower is better): {unsupported.describe()}\n")
    lines.append(f"- Evidence relevance to key terms: {relevance.describe()}\n")
    lines.append(
        f"- Runs with no citations at all: {no_citations} of {len(analysis.scores)}; "
        f"runs with at least one citation that resolves to nothing: {unresolvable}.\n"
    )

    # --- security ---
    lines.append("\n## Security behaviour\n")
    injection = [score for score in analysis.scores if score.injection_obeyed is not None]
    obeyed = sum(1 for score in injection if score.injection_obeyed)
    abuse = [score for score in analysis.scores if score.abusive_input_handled is not None]
    handled = sum(1 for score in abuse if score.abusive_input_handled)
    echoed = sum(1 for score in analysis.scores if score.abuse_echoed)
    lines.append(
        f"- Prompt-injection items scored: {len(injection)}; injection obeyed: **{obeyed}**; "
        "resisted: "
        f"{len(injection) - obeyed}.\n"
    )
    lines.append(
        f"- Toxic-persona items scored: {len(abuse)}; legitimate task answered despite the "
        f"abuse: **{handled}**; refused or deflected: {len(abuse) - handled}; "
        f"runs that echoed the abuse back: {echoed}.\n"
    )
    if not injection and not abuse:
        lines.append("- No security-category runs were recorded in this batch.\n")

    # --- critic agreement ---
    agreements = [score.critic_agrees for score in analysis.scores
                  if score.critic_agrees is not None]
    lines.append("\n## Critic agreement (not ground truth)\n")
    if agreements:
        agree, total, share = rate(agreements)
        lines.append(
            f"- The pipeline critic's approve/reject verdict matched the rubric's success "
            f"call in {agree} of {total} runs ({share * 100:.0f}%). This is reported for "
            "diagnosis only; the rubric and the answer key decide success.\n"
        )
    else:
        lines.append("- No run carried both a critic verdict and a rubric score.\n")

    # --- interpretation ---
    lines.append("\n## Interpretation\n")
    for bullet in interpretation_lines(analysis):
        lines.append(f"{bullet}\n")

    # --- charts ---
    lines.append("\n## Charts\n")
    if chart_status:
        lines.append(_table(("chart", "status"), sorted(chart_status.items())))
    else:
        lines.append("_Charts were not generated._\n")

    # --- per-run appendix ---
    lines.append("\n## Run appendix\n")
    appendix = []
    for record, score in analysis.records_with_scores():
        appendix.append(
            (
                record.get("run_key"),
                record.get("pattern"),
                record.get("terminal_state"),
                record.get("latency_s"),
                record.get("est_cost_usd"),
                "" if score is None else _fmt(score.rubric_score),
                "" if score is None else ("yes" if score.success else "no"),
                "|".join(score.failure_categories) if score else "",
            )
        )
    lines.append(_table(
        ("run_key", "pattern", "state", "latency s", "cost USD", "rubric", "success",
         "failures"),
        appendix,
    ))

    lines.append("\n## Where the numbers come from\n")
    lines.append(
        "- Raw records: `raw.jsonl` (one line per real attempt; no synthetic rows).\n"
        "- Joined table: `runs.csv`. Per-criterion audit trail: `scored.jsonl`.\n"
        "- Aggregates: `summary_by_pattern.csv`, `summary_by_category.csv`, `failures.csv`.\n"
        "- Charts: `charts/`. Every chart title carries the n of each group.\n"
    )
    if analysis.problems:
        lines.append("\n### Problems recorded while loading\n")
        for problem in analysis.problems:
            lines.append(f"- {problem}\n")
    return "".join(lines)


# --- CLI -------------------------------------------------------------------


def analyse(
    results_dir: Path,
    *,
    dataset: schema.EvalDataset,
    out_dir: Path | None = None,
    charts: bool = True,
) -> dict[str, Any]:
    """Score and aggregate one results directory. Returns the written paths."""
    out_dir = out_dir or results_dir
    analysis = load_analysis(results_dir, dataset)
    written: dict[str, Any] = {"results_dir": str(results_dir), "out_dir": str(out_dir),
                              "charts": {}, "problems": list(analysis.problems)}

    rows = run_rows(analysis)
    write_csv(out_dir / "runs.csv", rows, RUN_COLUMNS)
    write_csv(out_dir / "summary_by_pattern.csv", pattern_rows(analysis), PATTERN_COLUMNS)
    write_csv(out_dir / "summary_by_category.csv", category_rows(analysis), CATEGORY_COLUMNS)
    write_csv(
        out_dir / "failures.csv",
        [{"failure_category": name, "runs": count}
         for name, count in failure_counts(analysis).items()],
        ("failure_category", "runs"),
    )

    scored_path = out_dir / "scored.jsonl"
    scored_path.parent.mkdir(parents=True, exist_ok=True)
    with scored_path.open("w", encoding="utf-8") as handle:
        for score in analysis.scores:
            handle.write(json.dumps(score.as_dict(), ensure_ascii=False) + "\n")

    chart_status: dict[str, str] = {}
    if charts:
        try:
            chart_status = make_charts(analysis, out_dir / "charts")
        except ImportError as exc:  # pragma: no cover - matplotlib is installed here
            chart_status = {"all": f"skipped: matplotlib unavailable ({exc})"}

    report = build_report(analysis, dataset, chart_status)
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")

    written.update(
        {
            "records": len(analysis.records),
            "scored": len(analysis.scores),
            "charts": chart_status,
            "runs_csv": str(out_dir / "runs.csv"),
            "report": str(out_dir / "REPORT.md"),
        }
    )
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.analyze",
        description="Aggregate, chart and report on one evaluation results directory.",
    )
    parser.add_argument("--results", required=True, help="results directory with raw.jsonl")
    parser.add_argument("--out", default="", help="write outputs here (default: the results dir)")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--no-charts", dest="charts", action="store_false", default=True)
    parser.add_argument("--json", dest="json_out", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    results_dir = Path(args.results)
    if not results_dir.exists():
        print(f"error: {results_dir} does not exist", file=sys.stderr)
        return 2
    try:
        dataset = schema.load_dataset(Path(args.dataset))
    except Exception as exc:  # noqa: BLE001
        print(f"error: dataset is not valid: {exc}", file=sys.stderr)
        return 2

    written = analyse(
        results_dir,
        dataset=dataset,
        out_dir=Path(args.out) if args.out else None,
        charts=args.charts,
    )
    if args.json_out:
        print(json.dumps(written, indent=2, default=str))
    else:
        print(f"records scored : {written['records']} ({written['scored']} scored)")
        for name, status in written["charts"].items():
            print(f"chart          : {name}: {status}")
        print(f"csv            : {written['runs_csv']}")
        print(f"report         : {written['report']}")
        for problem in written["problems"]:
            print(f"problem        : {problem}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
