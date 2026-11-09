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

from evaluation import __version__, outcomes, schema, scoring
from evaluation.quarantine import QuarantinedPathError, assert_not_quarantined

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
    #: One entry per record, in record order: the score computed from *that*
    #: attempt, or None when it could not be scored. Pairing is decided inside
    #: the batch that produced both, so merging batches cannot make a legacy
    #: record ambiguous with an unrelated one from another batch.
    paired_scores: list[scoring.ItemScore | None] = field(default_factory=list)

    def records_with_scores(self) -> list[tuple[dict[str, Any], scoring.ItemScore | None]]:
        if len(self.paired_scores) == len(self.records):
            return list(zip(self.records, self.paired_scores))
        return pair_records_with_scores(self.records, self.scores)


def pair_records_with_scores(
    records: Sequence[Mapping[str, Any]],
    scores: Sequence[scoring.ItemScore],
) -> list[tuple[dict[str, Any], scoring.ItemScore | None]]:
    """Pair each record with the score computed from *that* execution.

    ``attempt_id`` identifies an execution, so it is the only key used when it
    is present. A run key that appears on several attempts therefore never
    collapses: a failed attempt keeps ``None`` even when a later attempt of the
    same key succeeded.

    The run-key fallback exists for records written before ``attempt_id``
    existed, and is deliberately narrow: it applies only when that run key
    identifies exactly one record and exactly one score. Anything ambiguous is
    left unpaired instead of being guessed.
    """
    by_attempt: dict[str, scoring.ItemScore | None] = {}
    for score in scores:
        if not score.attempt_id:
            continue
        # A duplicated attempt id is not an identity; refuse to attribute it.
        by_attempt[score.attempt_id] = (
            None if score.attempt_id in by_attempt else score
        )

    run_key_counts: dict[str, int] = {}
    attempt_counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("run_key") or "")
        run_key_counts[key] = run_key_counts.get(key, 0) + 1
        attempt_id = str(record.get("attempt_id") or "")
        if attempt_id:
            attempt_counts[attempt_id] = attempt_counts.get(attempt_id, 0) + 1

    legacy_by_key: dict[str, list[scoring.ItemScore]] = {}
    for score in scores:
        if score.attempt_id:
            continue
        legacy_by_key.setdefault(score.run_key, []).append(score)

    pairs: list[tuple[dict[str, Any], scoring.ItemScore | None]] = []
    for record in records:
        attempt_id = str(record.get("attempt_id") or "")
        if attempt_id:
            score = by_attempt.get(attempt_id) if attempt_counts[attempt_id] == 1 else None
            pairs.append((record, score))
            continue
        key = str(record.get("run_key") or "")
        candidates = legacy_by_key.get(key, [])
        if run_key_counts.get(key, 0) == 1 and len(candidates) == 1:
            pairs.append((record, candidates[0]))
        else:
            pairs.append((record, None))
    return pairs


def load_analysis(results_dir: Path, dataset: schema.EvalDataset) -> Analysis:
    assert_not_quarantined(results_dir)
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
    analysis.paired_scores = [
        score for _record, score in pair_records_with_scores(
            analysis.records, analysis.scores
        )
    ]
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

    # --- operational versus quality (see evaluation.outcomes) --------------
    # These answer different questions and must never be pooled into one rate:
    # "could the provider be reached" is not "was the research any good".
    started = [outcomes.model_started(record) for record in records]
    infra = [outcomes.classify(record).is_infrastructure for record in records]
    blocked = [outcomes.blocked_before_model(record) for record in records]
    interrupted = [outcomes.interrupted_after_model(record) for record in records]
    evaluable = [outcomes.critic_evaluable(record) for record in records]
    critic_evaluable_approvals = [
        record.get("critic_approved") is True
        for record in records
        if outcomes.critic_evaluable(record)
    ]
    valid = [outcomes.is_valid_completed(record) for record in records]
    # Quality is measured only over runs that actually produced research. When
    # none did, the mean is *undefined* - not 0.0, which would read as "the
    # pipeline scored zero" instead of "nothing was measured".
    valid_scores = [
        score.rubric_score
        for (record, score) in rows
        if score is not None and outcomes.is_valid_completed(record)
    ]
    quality = summarise(valid_scores)

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
        # --- the six explicit metrics ---
        "attempts": len(records),
        "model_start": rate(started),
        "research_completion": rate(completions),
        "critic_evaluable": (sum(evaluable), len(evaluable), None),
        "critic_evaluable_approval": rate(critic_evaluable_approvals),
        "scoring_success": rate(successes),
        "infrastructure_failure": rate(infra),
        "infrastructure_blocked": rate(blocked),
        "interrupted_after_model": rate(interrupted),
        "valid_completed": (sum(valid), len(valid), None),
        "research_quality": quality,
    }


def failure_counts(analysis: Analysis) -> dict[str, int]:
    counts: dict[str, int] = {}
    for score in analysis.scores:
        for name in score.failure_categories:
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def security_summary(scores: Sequence[scoring.ItemScore]) -> dict[str, Any]:
    """Security behaviour over the attempts whose answer could be judged.

    A run that produced no answer is **not** evidence that the agent resisted an
    injection or refused the abusive prompt: nothing was read. So each check
    keeps two counts - evaluable and unevaluable - and only the evaluable
    denominator is used for the rate. A failure is therefore never reported as a
    successful defence.
    """
    injection = [score for score in scores if score.category == "prompt_injection"]
    abuse = [score for score in scores if score.category == "toxic_persona"]

    injection_evaluable = [s for s in injection if s.injection_obeyed is not None]
    injection_obeyed = sum(1 for s in injection_evaluable if s.injection_obeyed)
    abuse_evaluable = [s for s in abuse if s.abusive_input_handled is not None]
    abuse_handled = sum(1 for s in abuse_evaluable if s.abusive_input_handled)
    abuse_echoed = sum(
        1 for s in abuse_evaluable if s.abuse_echoed
    )
    return {
        "injection_scored": len(injection),
        "injection_evaluable": len(injection_evaluable),
        "injection_unevaluable": len(injection) - len(injection_evaluable),
        "injection_obeyed": injection_obeyed,
        # Only ever a share of evaluable attempts; see the docstring.
        "injection_not_obeyed": len(injection_evaluable) - injection_obeyed,
        "abuse_scored": len(abuse),
        "abuse_evaluable": len(abuse_evaluable),
        "abuse_unevaluable": len(abuse) - len(abuse_evaluable),
        "abuse_handled": abuse_handled,
        # Not handled covers a refusal *and* a non-substantive answer; the rubric
        # cannot tell them apart, so the report does not call all of it refusal.
        "abuse_not_handled": len(abuse_evaluable) - abuse_handled,
        "abuse_echoed": abuse_echoed,
    }


def _security_rate_text(hits: int, total: int) -> str:
    """A security rate over its evaluable denominator, or an explicit undefined."""
    if not total:
        return RATE_UNDEFINED
    return f"{hits}/{total} ({hits / total * 100:.0f}%)"


def terminal_state_counts(analysis: Analysis) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in analysis.records:
        state = str(record.get("terminal_state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return dict(sorted(counts.items()))


# --- CSV -------------------------------------------------------------------


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> None:
    assert_not_quarantined(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


RUN_COLUMNS = (
    "run_key", "attempt_id", "attempt", "prompt_id", "category", "difficulty", "persona",
    "pattern", "context_sharing", "tier", "seed", "repeat", "terminal_state", "outcome",
    "latency_s", "tokens_in", "tokens_out", "tokens_total", "est_cost_usd", "cost_basis",
    "tool_calls", "recovery_rounds", "revision_rounds", "evidence_count",
    "citation_count", "resolved_citations", "confidence", "critic_status", "critic_approved",
    "rubric_score", "answer_key_coverage", "completeness", "citation_precision",
    "citation_coverage", "unsupported_claim_rate", "evidence_relevance", "success",
    "injection_obeyed", "abuse_echoed", "abusive_input_handled", "security_evaluable",
    "failure_categories", "errors",
)

PATTERN_COLUMNS = (
    "pattern", "runs", "scored", "completed_rate", "success_rate",
    "model_start_rate", "research_completion_rate", "critic_approval_rate",
    "scoring_success_rate", "infrastructure_failure_rate",
    "infrastructure_blocked_rate", "interrupted_after_model_rate",
    "critic_evaluable_n", "valid_completed_n", "research_quality_mean",
    "research_quality_n", "research_quality_note",
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
                "attempt_id": record.get("attempt_id"),
                "attempt": record.get("attempt"),
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
                    "" if score is None or score.unsupported_claim_rate is None
                    else round(score.unsupported_claim_rate, 4)
                ),
                "evidence_relevance": (
                    "" if score is None or score.evidence_relevance is None
                    else round(score.evidence_relevance, 4)
                ),
                "success": (score.success if score else None),
                "injection_obeyed": (score.injection_obeyed if score else None),
                "abuse_echoed": (score.abuse_echoed if score else None),
                "abusive_input_handled": (score.abusive_input_handled if score else None),
                "security_evaluable": (score.security_evaluable if score else None),
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
                **_metric_cells(stats),
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


#: Printed wherever a quality figure has no valid sample. Never "0".
QUALITY_UNDEFINED = "N/A - no valid completed research runs"
#: Printed when a rate had no eligible denominator at all.
RATE_UNDEFINED = "n/a (0 eligible)"


def quality_mean_text(stats: Stats) -> str:
    """The mean rubric over valid completed runs, or an explicit undefined.

    A batch in which no research run completed has *no* quality measurement. A
    zero there reads as "the pipeline scored nothing", which is a different and
    false claim, so this returns text rather than a number in that case.

    A *measured* zero is formatted explicitly as ``0.000`` so it cannot be
    mistaken for the undefined case, which is spelled out in words.
    """
    if stats.n == 0:
        return QUALITY_UNDEFINED
    return f"{stats.mean:.3f}"


def _metric_cells(stats: Mapping[str, Any]) -> dict[str, Any]:
    """The operational-versus-quality block every row carries."""
    hits, total, _ = stats["critic_evaluable"]
    vhits, vtotal, _ = stats["valid_completed"]
    quality = stats["research_quality"]

    def rate_or_undefined(triple: tuple[int, int, float | None]) -> str:
        # An empty denominator is "we could not measure this", not 0%.
        return _rate_text(triple) or RATE_UNDEFINED

    return {
        "model_start_rate": rate_or_undefined(stats["model_start"]),
        "research_completion_rate": rate_or_undefined(stats["research_completion"]),
        "critic_approval_rate": rate_or_undefined(stats["critic_evaluable_approval"]),
        "scoring_success_rate": rate_or_undefined(stats["scoring_success"]),
        "infrastructure_failure_rate": rate_or_undefined(stats["infrastructure_failure"]),
        "infrastructure_blocked_rate": rate_or_undefined(stats["infrastructure_blocked"]),
        "interrupted_after_model_rate": rate_or_undefined(stats["interrupted_after_model"]),
        "critic_evaluable_n": hits if total else 0,
        "valid_completed_n": vhits if vtotal else 0,
        "research_quality_mean": quality_mean_text(quality),
        "research_quality_n": quality.n,
        "research_quality_note": (
            "no valid completed research run: quality is undefined, not zero"
            if quality.n == 0
            else ""
        ),
    }


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
    assert_not_quarantined(path)
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
    assert_not_quarantined(path)
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


def pricing_metadata(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """The pricing facts of a batch, read from wherever the runner wrote them.

    ``run_meta.json`` carries two blocks with different jobs:

    ``pricing``
        Where the price table came from (``runner.pricing_status``).
    ``pricing_status``
        Whether *this batch's* models are priced, and whether the USD caps can
        bind (``runner.pricing_status_for_meta``). This is the canonical block.

    Older batches wrote ``pricing_summary`` (one model's price basis) instead of
    ``pricing_status``, so that key is accepted as a fallback rather than making
    an older batch's cost section say "unknown". Keys are returned as they are;
    a missing block stays empty instead of being invented.
    """
    meta = meta or {}
    table = meta.get("pricing") if isinstance(meta.get("pricing"), Mapping) else {}
    canonical = meta.get("pricing_status")
    if isinstance(canonical, Mapping) and canonical:
        status = dict(canonical)
        legacy = False
    else:
        fallback = meta.get("pricing_summary")
        status = dict(fallback) if isinstance(fallback, Mapping) else {}
        legacy = bool(status)
    budget = meta.get("budget")
    if status and isinstance(budget, Mapping) and budget.get("usd_cap_enforceable") is False:
        # Known rates cannot make a resumed ledger's unknown costs enforceable.
        status["usd_cap_enforceable"] = False
    return {"table": dict(table or {}), "status": status, "legacy": legacy}


def _pricing_rows(meta: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    """The "Bundled table" and "Configured model" cells of the cost table."""
    pricing = pricing_metadata(meta)
    table = pricing["table"]
    status = pricing["status"]
    if table:
        table_cell = (
            f"{table.get('table') or 'data/pricing.json'}"
            f" (as of {table.get('as_of') or 'unknown'}, "
            f"{table.get('entries', '?')} entries)"
        )
    else:
        table_cell = "unknown"

    if not status:
        model_cell = "unknown"
    else:
        label = status.get("model") or ", ".join(status.get("models") or []) or "unknown"
        basis = status.get("basis")
        if basis:
            model_cell = f"{label}: {basis}"
            if status.get("usd_cap_enforceable") is False:
                model_cell += "; USD caps cannot bound spend"
        elif status.get("priced") is True:
            model_cell = (
                f"{label}: priced, USD caps enforceable"
                if status.get("usd_cap_enforceable")
                else f"{label}: priced"
                + ("; USD caps cannot bound spend"
                   if status.get("usd_cap_enforceable") is False else "")
            )
        else:
            unpriced = ", ".join(status.get("unpriced_models") or []) or "unknown"
            model_cell = f"{label}: unpriced ({unpriced}); USD caps cannot bound spend"
    return (
        ("Bundled table", table_cell),
        ("Configured model", model_cell),
    )


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
        bases = sorted(
            {
                str(record.get("cost_basis") or "")
                for record in analysis.records
                if record.get("est_cost_usd") is None
            }
            - {""}
        )
        lines.append(
            f"- **Cost is unpriced, not zero.** {unpriced} of {len(analysis.records)} runs record "
            "`est_cost_usd = null`"
            + (f" because the configured model ({', '.join(models)}) has no applicable rate"
               if models else "")
            + (f"; recorded basis: {', '.join(f'`{basis}`' for basis in bases)}" if bases else "")
            + ". No total, median or per-pattern cost comparison is made: the cost columns are "
            "absent rather than filled with an invented rate. Token counts are the only "
            "comparable proxy for spend in this batch, and the provider's actual bill is not "
            "observable from here."
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
        pricing = pricing_metadata(meta)
        basis = pricing["status"].get("basis")
        lines.append(
            f"- {missing_cost} of {len(analysis.records)} runs carry **no cost estimate**. "
            + (f"Recorded basis: {basis}. " if basis else
               "See `run_meta.json -> pricing_status` for which models could not be priced. ")
            + "Unpriced runs are excluded from cost statistics, never counted as zero.\n"
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

    # --- operational versus research quality ---
    # Reported before the quality tables on purpose: a reader must know whether
    # the pipeline was even reachable before reading a success rate as a verdict
    # on the pipeline.
    lines.append("\n## Operational vs research quality\n")
    overall = summarise_group(analysis.records_with_scores())
    lines.append(_table(
        ("measure", "value", "what it means"),
        [
            ("attempts", overall["attempts"], "every recorded attempt"),
            ("model-start rate", _rate_text(overall["model_start"]),
             "runs with at least one recorded model call"),
            ("research completion", _rate_text(overall["research_completion"]),
             "terminal state `completed`"),
            ("critic approval", _rate_text(overall["critic_evaluable_approval"])
             or RATE_UNDEFINED,
             "approved / runs where the critic reached a verdict"),
            ("scoring success", _rate_text(overall["scoring_success"]),
             "scored runs that met the success rule"),
            ("infrastructure failures", _rate_text(overall["infrastructure_failure"]),
             "attempts ended by the environment, not the pipeline"),
            ("  blocked before a model", _rate_text(overall["infrastructure_blocked"]),
             "no model response was ever received"),
            ("  interrupted after a model", _rate_text(overall["interrupted_after_model"]),
             "started, then the environment stopped it"),
            ("research quality", quality_mean_text(overall["research_quality"]),
             "mean rubric over valid completed runs only"),
            ("quality sample (n)", overall["research_quality"].n,
             "valid completed research runs"),
        ],
    ))
    lines.append("\n")
    if overall["research_quality"].n == 0:
        lines.append(
            "**Research quality is undefined for this batch, not zero.** No run "
            "completed with a final report, so there is no quality measurement to "
            "report. The zero success rate above is an operational result, not a "
            "finding about research quality.\n"
        )
    lines.append(
        "Model-start rate is not execution success: a single model call does not "
        "establish that meaningful research happened.\n"
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
    if unsupported.missing:
        lines.append(
            f"- {unsupported.missing} scored run(s) produced no answer sentences, so the "
            "unsupported-claim rate is **not measured** for them. They are excluded from "
            "its n rather than counted as fully supported.\n"
        )
    lines.append(f"- Evidence relevance to key terms: {relevance.describe()}\n")
    lines.append(
        f"- Runs with no citations at all: {no_citations} of {len(analysis.scores)}; "
        f"runs with at least one citation that resolves to nothing: {unresolvable}.\n"
    )

    # --- security ---
    lines.append("\n## Security behaviour\n")
    security = security_summary(analysis.scores)
    if security["injection_scored"] or security["abuse_scored"]:
        lines.append(
            f"- Prompt-injection items scored: {security['injection_scored']}; "
            f"evaluable: **{security['injection_evaluable']}** "
            f"(unevaluable, no answer: {security['injection_unevaluable']}); "
            f"injection obeyed: **{security['injection_obeyed']}**; not obeyed: "
            f"{_security_rate_text(security['injection_not_obeyed'], security['injection_evaluable'])} "
            "of the evaluable attempts.\n"
        )
        lines.append(
            f"- Toxic-persona items scored: {security['abuse_scored']}; "
            f"evaluable: **{security['abuse_evaluable']}** "
            f"(unevaluable, no answer: {security['abuse_unevaluable']}); legitimate task "
            f"answered despite the abuse: **{security['abuse_handled']}**; refused, "
            f"deflected or answered non-substantively: "
            f"{_security_rate_text(security['abuse_not_handled'], security['abuse_evaluable'])} "
            f"of the evaluable attempts; runs that echoed the abuse back: "
            f"{security['abuse_echoed']}.\n"
        )
        if security["injection_unevaluable"] or security["abuse_unevaluable"]:
            lines.append(
                "- Attempts with no output are **unevaluable**, not resistant: a run "
                "that produced nothing is excluded from these denominators, and its "
                "failure stays in the operational tables above.\n"
            )
    else:
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
    assert_not_quarantined(results_dir)
    assert_not_quarantined(out_dir)
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
    assert_not_quarantined(scored_path)
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
    report_path = out_dir / "REPORT.md"
    assert_not_quarantined(report_path)
    report_path.write_text(report, encoding="utf-8")

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
    try:
        assert_not_quarantined(results_dir)
        if args.out:
            assert_not_quarantined(Path(args.out))
    except QuarantinedPathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not results_dir.exists():
        print(f"error: {results_dir} does not exist", file=sys.stderr)
        return 2
    try:
        dataset = schema.load_dataset(Path(args.dataset))
    except Exception as exc:  # noqa: BLE001
        print(f"error: dataset is not valid: {exc}", file=sys.stderr)
        return 2

    try:
        written = analyse(
            results_dir,
            dataset=dataset,
            out_dir=Path(args.out) if args.out else None,
            charts=args.charts,
        )
    except QuarantinedPathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
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
