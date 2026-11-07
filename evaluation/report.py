"""Write ``evaluation/REPORT.md`` from real, recorded results only.

    python -m evaluation.report                      # summarise every finished batch
    python -m evaluation.report --results evaluation/results/20250101T000000Z
    python -m evaluation.report --analyze             # also write CSVs + charts per batch

Why this module exists next to :mod:`evaluation.analyze`
-------------------------------------------------------
``analyze`` owns one results directory: it writes ``runs.csv``, the per-pattern
and per-category CSVs, ``charts/`` and that batch's own ``REPORT.md``. This module
owns the *repository-level* report: it reads every batch directory under
``evaluation/results/`` (or the ones you name), aggregates the records that were
actually written, and writes ``evaluation/REPORT.md``.

The two rules that matter:

* **No result is invented.** Every number below is computed from ``raw.jsonl``.
  A batch that recorded nothing contributes nothing. An empty results tree
  produces a report that says so and lists the commands that would fill it,
  rather than a template of plausible-looking figures.
* **No significance claims.** Groups are compared descriptively, with ``n``
  printed next to every aggregate, because a 72-run stratified batch cannot
  support a hypothesis test. :data:`evaluation.analyze.NO_SIGNIFICANCE_NOTE` is
  quoted verbatim so the wording cannot drift between the two reports.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from evaluation import __version__, analyze, schema, scoring

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_ROOT = REPO_ROOT / "evaluation" / "results"
DEFAULT_DATASET = REPO_ROOT / "evaluation" / "dataset.json"
DEFAULT_CONFIG = REPO_ROOT / "evaluation" / "config.json"
DEFAULT_OUT = REPO_ROOT / "evaluation" / "REPORT.md"

REPRODUCE_COMMANDS = (
    "python scripts/dev_up.py",
    "python -m evaluation.runner --dry-run",
    "python -m evaluation.runner --out evaluation/results/<UTC timestamp>",
    "python -m evaluation.analyze --results evaluation/results/<UTC timestamp>",
    "python -m evaluation.report",
)


# --- Discovery -------------------------------------------------------------


def has_records(directory: Path) -> bool:
    """A results directory counts when ``raw.jsonl`` exists and holds a line."""
    raw = directory / "raw.jsonl"
    if not raw.is_file():
        return False
    try:
        return any(line.strip() for line in raw.read_text(encoding="utf-8").splitlines())
    except OSError:
        return False


def _started_at(directory: Path) -> str:
    """Start time from ``run_meta.json`` when present, else the directory name."""
    meta_path = directory / "run_meta.json"
    if meta_path.is_file():
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            started = str(payload.get("started_at") or "")
            if started:
                return started
        except (OSError, ValueError):
            pass
    return directory.name


def find_results_dirs(root: Path) -> list[Path]:
    """Every results directory under ``root`` that actually recorded a run."""
    if not root.exists():
        return []
    if has_records(root):
        return [root]
    candidates = [path for path in root.iterdir() if path.is_dir() and has_records(path)]
    return sorted(candidates, key=_started_at)


# --- Aggregation -----------------------------------------------------------


def combine(analyses: Sequence[analyze.Analysis], root: Path) -> analyze.Analysis:
    """Merge several batches into one analysis.

    ``meta`` is taken from the most recent batch and flagged as such, so a single
    value (model, backend, plan) is never presented as if it applied to all of
    them; the batch table lists each one separately.
    """
    merged = analyze.Analysis(results_dir=root)
    for analysis in analyses:
        tag = analysis.results_dir.name
        merged.records.extend(analysis.records)
        merged.scores.extend(analysis.scores)
        merged.problems.extend(f"{tag}: {problem}" for problem in analysis.problems)
    if analyses:
        merged.meta = dict(analyses[-1].meta)
    return merged


def load_batches(directories: Iterable[Path], dataset: schema.EvalDataset) -> list[analyze.Analysis]:
    return [analyze.load_analysis(directory, dataset) for directory in directories]


def _cost_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    priced = sum(1 for record in records if isinstance(record.get("est_cost_usd"), (int, float)))
    return {"priced": priced, "unpriced": len(records) - priced}


def _total_cost(records: Sequence[Mapping[str, Any]]) -> float | None:
    values = [
        float(record["est_cost_usd"])
        for record in records
        if isinstance(record.get("est_cost_usd"), (int, float))
    ]
    return round(sum(values), 6) if values else None


def _all_failed(records: Sequence[Mapping[str, Any]]) -> bool:
    """True when a batch recorded attempts but not a single completed run.

    Such a batch says something about the environment (a quota, a missing key,
    an operator stopping it) and nothing about the pipeline, so the pooled
    headline has to point at it rather than let it dilute the success rate
    silently.
    """
    if not records:
        return False
    return not any(record.get("terminal_state") == "completed" for record in records)


def _distinct(records: Sequence[Mapping[str, Any]], key: str) -> list[str]:
    return sorted({str(record.get(key)) for record in records if record.get(key)})


def _models(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Model labels as each record's applied settings report them."""
    labels: set[str] = set()
    for record in records:
        label = str(record.get("model") or "")
        if not label:
            config = record.get("config")
            if isinstance(config, Mapping):
                label = str(config.get("model") or "")
        if label:
            labels.add(label)
    return sorted(labels)


# --- Report ----------------------------------------------------------------


def _no_results_report(
    dataset: schema.EvalDataset,
    config: schema.ExperimentConfig | None,
    directories: Sequence[Path],
    root: Path,
) -> str:
    """What to say when nothing has been measured. Numbers about the *plan* only."""
    lines: list[str] = [
        "# Athena evaluation report\n",
        f"\nGenerated by `python -m evaluation.report` (harness v{__version__}).\n",
        "\n## Status: no runs have been recorded yet\n",
        (
            "\nThere is no `raw.jsonl` with records under "
            f"`{_rel(root)}`, so this report contains **no results**. Every table that "
            "would compare patterns, categories, latency, cost or citation quality is "
            "omitted on purpose: producing it now would mean inventing the numbers.\n"
        ),
    ]
    if directories:
        lines.append(
            f"\nResults directories examined: {len(directories)} - "
            + ", ".join(f"`{_rel(path)}`" for path in directories)
            + ". None contained a record.\n"
        )
    else:
        lines.append(f"\nResults directory: `{_rel(root)}` (does not exist yet).\n")

    lines.append("\n## What is ready to run (plan metadata, not results)\n")
    lines.append(
        "\nThese facts describe the committed harness, not any measured behaviour.\n\n"
    )
    counts = dataset.category_counts()
    rows = [
        ("Prompts in `evaluation/dataset.json`", len(dataset.items)),
        ("Categories covered", f"{sum(1 for value in counts.values() if value)} of {len(schema.CATEGORIES)}"),
        (
            "Prompts per category",
            ", ".join(f"{name}={value}" for name, value in sorted(counts.items())),
        ),
    ]
    if config is not None:
        rows.extend(
            [
                ("Planned runs (`config.json`)", len(config.plan.entries)),
                (
                    "Runs per pattern",
                    ", ".join(
                        f"{name}={value}" for name, value in sorted(config.plan.patterns().items())
                    ),
                ),
                (
                    "Tiers",
                    ", ".join(
                        f"{name}={sum(1 for entry in config.plan.entries if entry.tier == name)}"
                        for name in ("core", "breadth", "variance")
                    ),
                ),
                ("Per-run timeout", f"{config.matrix.per_run_timeout_s:.0f} s"),
                (
                    "Budget caps",
                    f"<= {config.budget.max_runs} runs, "
                    f"<= {config.budget.max_total_usd} USD total, "
                    f"<= {config.budget.max_run_usd} USD per run",
                ),
            ]
        )
    lines.append(analyze._table(("Item", "Value"), rows))

    lines.append("\n## How to produce the results\n")
    lines.append(
        "\nThe crew A2A server must be up (`python scripts/dev_up.py`) before a live "
        "batch will start; the runner refuses to launch without it and reports why.\n"
    )
    lines.append("\n```\n" + "\n".join(REPRODUCE_COMMANDS) + "\n```\n")
    lines.append(
        "\nThen rerun `python -m evaluation.report`: this file is regenerated from the "
        "records that exist at that moment.\n"
    )
    return "".join(lines)


def build_overview(
    analyses: Sequence[analyze.Analysis],
    merged: analyze.Analysis,
    dataset: schema.EvalDataset,
    config: schema.ExperimentConfig | None,
    root: Path,
) -> str:
    """Render ``evaluation/REPORT.md`` from the merged real records."""
    if not merged.records:
        return _no_results_report(dataset, config, [a.results_dir for a in analyses], root)

    records = merged.records
    scores = merged.scores
    lines: list[str] = [
        "# Athena evaluation report\n",
        f"\nGenerated by `python -m evaluation.report` (harness v{__version__}) from "
        f"{len(records)} recorded run(s).\n",
        f"\nResults root: `{_rel(root)}` - {len(analyses)} batch director"
        f"{'y' if len(analyses) == 1 else 'ies'}.\n",
    ]

    # --- headline ------------------------------------------------------
    lines.append("\n## Read this first\n")
    failed_batches = [
        a for a in analyses if _all_failed(a.records)
    ]
    lines.append(
        "- The headline below pools every batch directory it can find. It is "
        "**not** one experiment, and the pooled success rate must not be quoted "
        "as a measure of research quality.\n"
    )
    lines.append(
        "- A run that failed before any model call (an exhausted provider quota, "
        "a missing key, a stopped batch) counts as `failed` and drags the pooled "
        "share down without saying anything about the pipeline. Read the "
        "per-batch table and the outcome notes next to each batch directory "
        "before drawing conclusions.\n"
    )
    if failed_batches:
        names = ", ".join(f"`{_rel(a.results_dir)}`" for a in failed_batches)
        lines.append(
            f"- {len(failed_batches)} batch(es) recorded **only** failures: {names}. "
            "See the `*.md` outcome note inside each for why.\n"
        )

    hits, total, share = analyze.rate(score.success for score in scores)
    prices = _cost_counts(records)
    lines.append("\n## Headline\n")
    headline = [
        ("Runs recorded (one per real attempt)", len(records)),
        ("Runs scored against the dataset", len(scores)),
        (
            "Success",
            f"{hits}/{total} ({share * 100:.0f}%)" if total else "no scored runs",
        ),
        ("Terminal states", ", ".join(
            f"{state}={count}" for state, count in analyze.terminal_state_counts(merged).items()
        )),
        ("Models used", ", ".join(_models(records)) or "(not recorded per run)"),
        ("Patterns used", ", ".join(_distinct(records, "pattern"))),
        ("Cost estimates", f"{prices['priced']} priced, {prices['unpriced']} unpriced"),
        (
            "Total estimated cost",
            f"{analyze._fmt(_total_cost(records))} USD (list price estimate, not a bill)"
            if prices["priced"] else "none recorded",
        ),
    ]
    lines.append(analyze._table(("Measure", "Value"), headline))

    # --- operational vs research quality -------------------------------
    # Pooled across batches, so it must be read as an environment summary
    # rather than one experiment. The quality row is explicitly undefined when
    # no run in any pooled batch completed - a pooled 0 would be a false claim.
    pooled = analyze.summarise_group(merged.records_with_scores())
    lines.append("\n## Operational vs research quality (all batches pooled)\n")
    lines.append(analyze._table(
        ("measure", "value", "what it means"),
        [
            ("attempts", pooled["attempts"], "every recorded attempt"),
            ("model-start rate", analyze._rate_text(pooled["model_start"]),
             "at least one recorded model call; not execution success"),
            ("research completion", analyze._rate_text(pooled["research_completion"]),
             "terminal state `completed`"),
            ("critic approval",
             analyze._rate_text(pooled["critic_evaluable_approval"])
             or analyze.RATE_UNDEFINED,
             "approved / runs where the critic reached a verdict"),
            ("scoring success", analyze._rate_text(pooled["scoring_success"]),
             "scored runs meeting the success rule"),
            ("infrastructure failures",
             analyze._rate_text(pooled["infrastructure_failure"]),
             "ended by the environment, not the pipeline"),
            ("  blocked before a model",
             analyze._rate_text(pooled["infrastructure_blocked"]),
             "no model response was ever received"),
            ("  interrupted after a model",
             analyze._rate_text(pooled["interrupted_after_model"]),
             "started, then the environment stopped it"),
            ("research quality", analyze.quality_mean_text(pooled["research_quality"]),
             "mean rubric over valid completed runs only"),
            ("quality sample (n)", pooled["research_quality"].n,
             "valid completed research runs"),
        ],
    ))
    lines.append("\n")

    # --- batches -------------------------------------------------------
    lines.append("\n## Batches in this report\n")
    batch_rows = []
    for analysis in analyses:
        meta = analysis.meta
        states = analyze.terminal_state_counts(analysis)
        batch_rows.append(
            (
                f"`{_rel(analysis.results_dir)}`",
                meta.get("plan_id", "-") or "-",
                meta.get("backend", "-") or "-",
                meta.get("model", "-") or "-",
                meta.get("started_at", "-") or "-",
                meta.get("ended_at", "-") or "(not finished)",
                meta.get("stopped_reason") or "none",
                len(analysis.records),
                ", ".join(f"{state}={count}" for state, count in states.items()) or "-",
            )
        )
    lines.append(
        analyze._table(
            ("batch", "plan", "backend", "model", "started", "ended", "stopped",
             "records", "states"),
            batch_rows,
        )
    )

    # --- caveats -------------------------------------------------------
    lines.append("\n## Caveats to read before the tables\n")
    lines.append(f"- {analyze.NO_SIGNIFICANCE_NOTE}\n")
    per_pattern = analyze.group_by_pattern(merged)
    per_category = analyze.group_by_category(merged)
    lines.append(
        "- Runs per pattern: "
        + ", ".join(f"{name}={len(rows)}" for name, rows in per_pattern.items())
        + ".\n"
    )
    if prices["unpriced"]:
        lines.append(
            f"- {prices['unpriced']} of {len(records)} runs carry **no cost estimate**: the "
            "model has no rate in `evaluation/config.json -> pricing` or in the bundled "
            "`data/pricing.json` table. Unpriced runs are excluded from cost statistics, "
            "never counted as zero.\n"
        )
    volatile = {item.id for item in dataset.items if item.answer_key.volatility == "volatile"}
    volatile_runs = [score for score in scores if score.prompt_id in volatile]
    if volatile_runs:
        lines.append(
            f"- {len(volatile_runs)} scored run(s) are volatile items: they are judged on "
            "sourcing, dating and structure, not on a memorised value.\n"
        )
    if merged.problems:
        lines.append(
            f"- {len(merged.problems)} record(s) or file(s) could not be read or scored; "
            "they are listed at the end of this report and are excluded from the tables.\n"
        )
    lines.append(
        "- The pipeline's own critic verdict is recorded as `critic_acceptance` and is not "
        f"used to decide success (`scoring.CRITIC_IS_GROUND_TRUTH = "
        f"{scoring.CRITIC_IS_GROUND_TRUTH}`).\n"
    )
    lines.append(
        "- Success means: terminal state `completed`, a non-empty final answer, rubric score "
        f">= {scoring.SUCCESS_THRESHOLD}, no citation that resolves to nothing, the item's "
        "required evidence cited (or the gap stated honestly), and no security violation.\n"
    )

    # --- by pattern ----------------------------------------------------
    lines.append("\n## By pattern\n")
    pattern_rows = []
    for name, rows in per_pattern.items():
        stats = analyze.summarise_group(rows)
        cost_prices = _cost_counts([record for record, _score in rows])
        pattern_rows.append(
            (
                name,
                stats["runs"],
                stats["scored"],
                analyze._rate_text(stats["success"]),
                stats["latency"].describe("s"),
                stats["tokens"].describe("tokens"),
                (
                    f"n={stats['cost'].n}, median {analyze._fmt(stats['cost'].median)} USD "
                    f"({cost_prices['unpriced']} unpriced)"
                    if stats["cost"].n else f"no priced runs ({cost_prices['unpriced']} unpriced)"
                ),
                f"{analyze._fmt(stats['rubric'].median)} (n={stats['rubric'].n})",
                f"{analyze._fmt(stats['citation_precision'].median)} "
                f"(n={stats['citation_precision'].n})",
                f"{analyze._fmt(stats['citation_coverage'].median)} "
                f"(n={stats['citation_coverage'].n})",
                analyze._rate_text(stats["critic_approved"]),
            )
        )
    lines.append(
        analyze._table(
            ("pattern", "runs", "scored", "success", "latency", "tokens", "cost",
             "rubric median", "citation precision", "required-evidence coverage",
             "critic approved"),
            pattern_rows,
        )
    )

    # --- by category ---------------------------------------------------
    lines.append("\n## By category\n")
    category_rows = []
    for name, rows in per_category.items():
        stats = analyze.summarise_group(rows)
        local: dict[str, int] = {}
        for _record, score in rows:
            if score is None:
                continue
            for failure in score.failure_categories:
                local[failure] = local.get(failure, 0) + 1
        top = max(local.items(), key=lambda pair: pair[1])[0] if local else "-"
        category_rows.append(
            (
                name,
                stats["runs"],
                stats["scored"],
                analyze._rate_text(stats["success"]),
                f"{analyze._fmt(stats['rubric'].median)} (n={stats['rubric'].n})",
                f"{analyze._fmt(stats['answer_key'].median)} (n={stats['answer_key'].n})",
                f"{analyze._fmt(stats['citation_precision'].median)} "
                f"(n={stats['citation_precision'].n})",
                f"{analyze._fmt(stats['latency'].median)} s (n={stats['latency'].n})",
                top,
            )
        )
    lines.append(
        analyze._table(
            ("category", "runs", "scored", "success", "rubric median",
             "answer-key coverage", "citation precision", "latency median",
             "most common failure"),
            category_rows,
        )
    )

    # --- quality -------------------------------------------------------
    lines.append("\n## Answer quality\n")
    quality = [
        ("Rubric score (0-1)", analyze.summarise(score.rubric_score for score in scores)),
        ("Answer-key coverage", analyze.summarise(score.answer_key_coverage for score in scores)),
        ("Completeness", analyze.summarise(score.completeness for score in scores)),
        (
            "Unsupported-claim rate (lower is better)",
            analyze.summarise(score.unsupported_claim_rate for score in scores),
        ),
        (
            "Evidence relevance to key terms",
            analyze.summarise(score.evidence_relevance for score in scores),
        ),
    ]
    lines.append(
        analyze._table(
            ("measure", "distribution"),
            [(name, stats.describe()) for name, stats in quality],
        )
    )

    # --- citations -----------------------------------------------------
    lines.append("\n## Citation quality\n")
    precision = analyze.summarise(score.citation_precision for score in scores)
    coverage = analyze.summarise(score.citation_coverage for score in scores)
    produced = sum(score.citation_count for score in scores)
    resolved = sum(score.resolved_citations for score in scores)
    no_citations = sum(1 for score in scores if score.citation_count == 0)
    unresolved = sum(
        1 for score in scores if score.citation_count and score.citation_precision < 1.0
    )
    missing_evidence = sum(1 for score in scores if score.required_evidence_missing)
    lines.append(
        analyze._table(
            ("measure", "value"),
            [
                ("Citations produced / resolved to captured evidence", f"{produced} / {resolved}"),
                ("Citation precision (resolved / produced)", precision.describe()),
                ("Required-evidence coverage", coverage.describe()),
                (
                    "Runs with no citations at all",
                    f"{no_citations} of {len(scores)}",
                ),
                (
                    "Runs with at least one citation that resolves to nothing",
                    f"{unresolved} of {len(scores)}",
                ),
                (
                    "Runs missing a required evidence kind",
                    f"{missing_evidence} of {len(scores)}",
                ),
            ],
        )
    )

    # --- security ------------------------------------------------------
    lines.append("\n## Security behaviour\n")
    injection = [score for score in scores if score.injection_obeyed is not None]
    obeyed = sum(1 for score in injection if score.injection_obeyed)
    abuse = [score for score in scores if score.abusive_input_handled is not None]
    handled = sum(1 for score in abuse if score.abusive_input_handled)
    echoed = sum(1 for score in scores if score.abuse_echoed)
    if injection or abuse:
        lines.append(
            analyze._table(
                ("check", "result"),
                [
                    (
                        "Prompt-injection items scored / injection obeyed",
                        f"{len(injection)} / {obeyed}",
                    ),
                    (
                        "Injection resisted",
                        f"{len(injection) - obeyed} of {len(injection)}",
                    ),
                    (
                        "Toxic-persona items scored / legitimate task still answered",
                        f"{len(abuse)} / {handled}",
                    ),
                    ("Runs that echoed the abuse back", f"{echoed}"),
                ],
            )
        )
    else:
        lines.append("- No prompt-injection or toxic-persona runs were recorded in these batches.\n")

    # --- critic --------------------------------------------------------
    lines.append("\n## Critic agreement (diagnosis only)\n")
    agreements = [score.critic_agrees for score in scores if score.critic_agrees is not None]
    if agreements:
        agree, total_agreements, share_agreements = analyze.rate(agreements)
        lines.append(
            f"- The pipeline critic's approve/reject verdict matched the rubric's success call "
            f"in {agree} of {total_agreements} runs ({share_agreements * 100:.0f}%). This is "
            "reported for diagnosis; the rubric and the answer key decide success.\n"
        )
    else:
        lines.append("- No run carried both a critic verdict and a rubric score.\n")

    # --- failures ------------------------------------------------------
    lines.append("\n## Failure taxonomy\n")
    failures = analyze.failure_counts(merged)
    if failures:
        denominator = max(1, len(scores))
        lines.append(
            analyze._table(
                ("failure category", "runs", "share of scored runs"),
                [
                    (name, count, f"{count / denominator * 100:.0f}%")
                    for name, count in failures.items()
                ],
            )
        )
        lines.append("\nA run can carry more than one failure label.\n")
    else:
        lines.append("- No failure labels were assigned.\n")

    # --- cost ----------------------------------------------------------
    lines.append("\n## Cost accounting\n")
    pricing = (merged.meta or {}).get("pricing") or {}
    pricing_summary = (merged.meta or {}).get("pricing_summary") or {}
    cost_stats = analyze.summarise(record.get("est_cost_usd") for record in records)
    median_cost = (
        f"{analyze._fmt(cost_stats.median)} USD (n={cost_stats.n})"
        if cost_stats.n else "none recorded"
    )
    cost_rows = [
        ("Runs with a cost estimate", f"{prices['priced']} of {len(records)}"),
        (
            "Total estimate",
            f"{analyze._fmt(_total_cost(records))} USD" if prices["priced"] else "none recorded",
        ),
        ("Median cost per priced run", median_cost),
        ("Price basis", "list price per million tokens, estimate only - not a bill"),
        (
            "Bundled table",
            (
                f"{pricing.get('table') or 'data/pricing.json'}"
                f" (as of {pricing.get('as_of') or 'unknown'}, "
                f"{pricing.get('entries', '?')} entries)"
            ) if pricing else "unknown",
        ),
        (
            "Configured model",
            (
                f"{pricing_summary.get('model')}: {pricing_summary.get('basis')}"
            ) if pricing_summary else "unknown",
        ),
    ]
    lines.append(analyze._table(("measure", "value"), cost_rows))

    # --- interpretation -------------------------------------------------
    lines.append("\n## Interpretation\n")
    for bullet in analyze.interpretation_lines(merged):
        lines.append(f"{bullet}\n")

    # --- per-run appendix ----------------------------------------------
    lines.append("\n## Run appendix\n")
    appendix = []
    for record, score in merged.records_with_scores():
        appendix.append(
            (
                record.get("run_key"),
                record.get("prompt_id"),
                record.get("pattern"),
                record.get("terminal_state"),
                record.get("latency_s"),
                (
                    analyze._fmt(record.get("est_cost_usd"))
                    if isinstance(record.get("est_cost_usd"), (int, float))
                    else "unpriced"
                ),
                "-" if score is None else analyze._fmt(score.rubric_score),
                "-" if score is None else ("yes" if score.success else "no"),
                "|".join(score.failure_categories) if score else "",
            )
        )
    lines.append(
        analyze._table(
            ("run_key", "prompt", "pattern", "state", "latency s", "cost USD",
             "rubric", "success", "failures"),
            appendix,
        )
    )

    # --- artifacts -----------------------------------------------------
    lines.append("\n## Where the numbers come from\n")
    lines.append(
        "- Raw records: `raw.jsonl` in each batch directory (one line per real attempt; "
        "a run that did not happen leaves no line).\n"
        "- Joined per-run table, per-criterion audit trail and charts are produced per batch "
        "by `python -m evaluation.analyze --results <batch>`: `runs.csv`, `scored.jsonl`, "
        "`summary_by_pattern.csv`, `summary_by_category.csv`, `failures.csv`, `charts/`.\n"
        "- This file aggregates those same records across batches; it does not contain a "
        "number that is not in `raw.jsonl`.\n"
    )
    for analysis in analyses:
        lines.append(
            f"- `{_rel(analysis.results_dir)}`: {len(analysis.records)} record(s); "
            f"per-batch report at `{_rel(analysis.results_dir / 'REPORT.md')}` "
            f"({'present' if (analysis.results_dir / 'REPORT.md').exists() else 'not generated yet'}).\n"
        )

    if merged.problems:
        lines.append("\n## Problems recorded while loading\n")
        for problem in merged.problems:
            lines.append(f"- {problem}\n")

    lines.append("\n## Reproduce\n")
    lines.append("\n```\n" + "\n".join(REPRODUCE_COMMANDS) + "\n```\n")
    return "".join(lines)


def _rel(path: Path) -> str:
    """Repo-relative path where possible, so the report is portable."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except Exception:  # noqa: BLE001 - a path outside the repo is shown as given
        return str(path)


# --- Entry points ----------------------------------------------------------


def write_report(
    *,
    results_root: Path = DEFAULT_RESULTS_ROOT,
    out_path: Path = DEFAULT_OUT,
    dataset: schema.EvalDataset | None = None,
    config: schema.ExperimentConfig | None = None,
    results_dirs: Sequence[Path] | None = None,
    with_artifacts: bool = False,
) -> dict[str, Any]:
    """Build and write the report. Returns what was written, for the CLI/tests."""
    dataset = dataset or schema.load_dataset(DEFAULT_DATASET)
    if config is None:
        try:
            config = schema.load_config(DEFAULT_CONFIG)
        except Exception:  # noqa: BLE001 - the plan table is optional in the report
            config = None

    directories = (
        list(results_dirs) if results_dirs is not None else find_results_dirs(results_root)
    )
    analyses = load_batches(directories, dataset)
    merged = combine(analyses, results_root)

    artifacts: dict[str, Any] = {}
    if with_artifacts:
        for analysis in analyses:
            artifacts[str(analysis.results_dir)] = analyze.analyse(
                analysis.results_dir, dataset=dataset
            )

    text = build_overview(analyses, merged, dataset, config, results_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return {
        "report": str(out_path),
        "results_root": str(results_root),
        "batches": len(analyses),
        "records": len(merged.records),
        "scored": len(merged.scores),
        "problems": list(merged.problems),
        "artifacts": artifacts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.report",
        description="Write evaluation/REPORT.md from the results that actually exist.",
    )
    parser.add_argument(
        "--results", action="append", default=None,
        help="a results directory (repeatable); default: every finished batch under "
             "evaluation/results/",
    )
    parser.add_argument(
        "--root", default=str(DEFAULT_RESULTS_ROOT),
        help="results root to scan when --results is not given",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="where to write REPORT.md")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument(
        "--analyze", dest="with_artifacts", action="store_true",
        help="also run evaluation.analyze on each batch (CSVs and charts)",
    )
    parser.add_argument("--json", dest="json_out", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    try:
        dataset = schema.load_dataset(Path(args.dataset))
    except Exception as exc:  # noqa: BLE001 - pydantic's message is the report
        print(f"error: dataset is not valid: {exc}", file=sys.stderr)
        return 2

    results_dirs = [Path(item) for item in args.results] if args.results else None
    written = write_report(
        results_root=Path(args.root),
        out_path=Path(args.out),
        dataset=dataset,
        results_dirs=results_dirs,
        with_artifacts=args.with_artifacts,
    )
    if args.json_out:
        print(json.dumps(written, indent=2, default=str))
    else:
        print(f"batches  : {written['batches']}")
        print(f"records  : {written['records']} ({written['scored']} scored)")
        print(f"report   : {written['report']}")
        for problem in written["problems"]:
            print(f"problem  : {problem}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
