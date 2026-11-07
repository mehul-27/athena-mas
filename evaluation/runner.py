"""Run the evaluation plan and record exactly what happened.

    python -m evaluation.runner --dry-run
    python -m evaluation.runner --out evaluation/results/20250101T000000Z
    python -m evaluation.runner --plan evaluation/config.json --out evaluation/results/<ts>

Design rules this file follows, because an evaluation harness is worthless if it
can quietly lie:

* One JSONL line per **real attempt**. A run that did not happen produces no
  record; a run that failed produces a record saying so.
* Nothing is estimated into existence. Token counts come from the event store,
  cost comes from the pricing table in ``config.json`` or stays ``null``, and the
  final answer comes from the blackboard ``final_report``.
* A timeout or an exception is recorded and the batch continues.
* The batch resumes: keys already present in ``raw.jsonl`` are skipped, so an
  interrupted 72-run batch does not start over.
* ``--dry-run`` writes nothing at all.

The preferred execution path is in-process (``athena.run.run_one``). The
``subprocess`` backend exists because that is exactly what the demo HTTP API does
(``athena/api/runs.py`` spawns ``python -m athena.run``), so the fallback is the
same code path the web UI uses.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from evaluation import __version__, schema
from evaluation.schema import EvalDataset, EvalItem, ExperimentConfig, PlanEntry, RunRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "evaluation" / "config.json"
DEFAULT_DATASET = REPO_ROOT / "evaluation" / "dataset.json"
DEFAULT_RESULTS_ROOT = REPO_ROOT / "evaluation" / "results"

#: Where the demo API keeps its per-run summaries. The subprocess backend reads
#: this only as a fallback, so no file outside the results directory is written.
RUN_SUMMARY_DIR = REPO_ROOT / ".run" / "runs"

#: Evidence text is trimmed in the record so ``raw.jsonl`` stays readable; the
#: scorer only needs enough text to measure overlap.
EVIDENCE_CHAR_LIMIT = 600

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_PRECONDITION = 3
EXIT_INTERRUPTED = 130


# --- Small utilities -------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest(path: Path | None) -> str | None:
    """Sha256 of a provenance file, or None when it is not readable."""
    if path is None:
        return None
    try:
        return sha256_of(Path(path))
    except OSError:
        return None


def git_commit() -> str:
    """Best-effort commit id; never fails a batch."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=5, check=False,
        )
        return result.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - provenance is nice to have, not required
        return "unknown"


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            return dict(value.model_dump())
        except Exception:  # noqa: BLE001 - a bad model must not break recording
            return {}
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


# --- Cost ------------------------------------------------------------------


def resolve_price(config: ExperimentConfig, model: str) -> tuple[schema.PriceEntry | None, str]:
    """Find the price entry for ``model``: exact, then substring, then default."""
    models = config.pricing.models
    if not models:
        return None, "no pricing table"
    key = (model or "").strip()
    if key and key in models:
        return models[key], key
    lowered = key.lower()
    for name, entry in models.items():
        if name == "default":
            continue
        if lowered and name.lower() in lowered:
            return entry, name
    if "default" in models:
        return models["default"], "default"
    return None, "no matching price"


def catalog_estimate(
    model: str, tokens_in: int, tokens_out: int
) -> dict[str, Any] | None:
    """Price one run from the repo's bundled list-price table.

    ``athena.observability.cost`` reads ``data/pricing.json`` (or
    ``$ATHENA_PRICING``) and returns ``cost_usd=None`` for a model it does not
    know. When that module or its table is missing this returns ``None`` so the
    caller can say "unpriced" instead of guessing a rate.
    """
    try:
        from athena.observability.cost import estimate_cost as catalog_cost

        estimate = catalog_cost(model, tokens_in, tokens_out)
    except Exception:  # noqa: BLE001 - pricing is an extra, never a precondition
        return None
    if estimate.cost_usd is None:
        return None
    matched = estimate.matched_pattern or estimate.model or model
    return {
        "cost_usd": round(float(estimate.cost_usd), 6),
        "basis": f"priced:{matched} (data/pricing.json)",
        "as_of": estimate.priced_as_of,
        "matched_pattern": estimate.matched_pattern,
        "provider": estimate.provider,
    }


def pricing_status() -> dict[str, Any]:
    """Where prices come from, for ``run_meta.json`` and the dry run."""
    try:
        from athena.observability.cost import pricing_meta

        meta = pricing_meta()
        return {
            "source": "athena.observability.cost",
            "table": meta.get("table"),
            "as_of": meta.get("as_of"),
            "entries": meta.get("entries"),
            "unit": meta.get("unit"),
            "unknown_model_basis": meta.get("unknown_model_basis"),
        }
    except Exception as exc:  # noqa: BLE001 - a missing table is reported, not fatal
        return {
            "source": "evaluation/config.json pricing only",
            "error": f"{type(exc).__name__}: {exc}",
        }


def pricing_summary(config: ExperimentConfig, model: str) -> dict[str, Any]:
    """Will this model's runs carry a cost estimate, and from which table?"""
    entry, basis = resolve_price(config, model)
    if entry is not None:
        return {"model": model, "priced": True, "basis": f"config.pricing:{basis}"}
    probe = catalog_estimate(model, 1_000_000, 0)
    if probe is not None:
        return {"model": model, "priced": True, "basis": probe["basis"]}
    return {
        "model": model,
        "priced": False,
        "basis": (
            "unpriced: no entry in evaluation/config.json pricing or data/pricing.json, "
            "so cost is recorded as null"
        ),
    }


def estimate_cost(
    config: ExperimentConfig,
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
) -> tuple[float | None, str]:
    """USD estimate for one run, or ``(None, reason)`` when it cannot be priced.

    ``config.json -> pricing`` is consulted first because it is committed with the
    plan; the repo's bundled table (``athena.observability.cost``) is the
    fallback. A model in neither table stays unpriced: no rate is interpolated.
    """
    if tokens_in is None and tokens_out is None:
        return None, "no token counts recorded"
    tokens_in = max(int(tokens_in or 0), 0)
    tokens_out = max(int(tokens_out or 0), 0)

    entry, basis = resolve_price(config, model)
    if entry is not None:
        cost = (
            (tokens_in / 1_000_000) * entry.input
            + (tokens_out / 1_000_000) * entry.output
        )
        return round(cost, 6), f"priced:{basis}"

    catalog = catalog_estimate(model, tokens_in, tokens_out)
    if catalog is not None:
        return catalog["cost_usd"], catalog["basis"]
    return None, "unknown_model"


# --- Result capture --------------------------------------------------------


def blackboard_sections(run_id: str) -> dict[str, Any]:
    """Read the run's blackboard sections, or an empty dict if unavailable."""
    try:
        from athena.blackboard import Blackboard

        snapshot = Blackboard.open(run_id).snapshot()
        return dict(snapshot.get("sections") or {})
    except Exception:  # noqa: BLE001 - a missing blackboard is a finding, not a crash
        return {}


def event_totals(run_id: str) -> dict[str, Any] | None:
    """Token and call totals from the monitoring event store."""
    try:
        from athena.observability.store import SQLiteEventStore

        events = SQLiteEventStore.from_env().events(run_id)
    except Exception:  # noqa: BLE001
        return None
    if not events:
        return None

    totals = {
        "tokens_in": 0,
        "tokens_out": 0,
        "reasoning_tokens": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "recovery_events": 0,
        "errors": 0,
    }
    for event in events:
        kind = getattr(event, "kind", "")
        if kind == "model_call":
            totals["model_calls"] += 1
            totals["tokens_in"] += int(getattr(event, "tokens_in", 0) or 0)
            totals["tokens_out"] += int(getattr(event, "tokens_out", 0) or 0)
            totals["reasoning_tokens"] += int((getattr(event, "extra", {}) or {}).get(
                "reasoning_tokens") or 0)
        elif kind == "a2a_call":
            totals["tokens_in"] += int(getattr(event, "tokens_in", 0) or 0)
            totals["tokens_out"] += int(getattr(event, "tokens_out", 0) or 0)
            totals["reasoning_tokens"] += int((getattr(event, "extra", {}) or {}).get(
                "reasoning_tokens") or 0)
        elif kind == "tool_call":
            totals["tool_calls"] += 1
        elif kind == "recovery":
            totals["recovery_events"] += 1
        elif kind == "error":
            totals["errors"] += 1
    return totals


def capture_report(sections: Mapping[str, Any]) -> dict[str, Any]:
    """Pull answer, key points, citations and guardrail flags out of the blackboard."""
    report = sections.get("final_report")
    if isinstance(report, (list, tuple)):  # list sections never hold the report
        report = report[0] if report else None
    payload = _as_mapping(report)

    citations = []
    for citation in payload.get("citations") or []:
        entry = _as_mapping(citation)
        citations.append(
            {
                "source_type": str(entry.get("source_type") or ""),
                "source_ref": str(entry.get("source_ref") or ""),
                "quote_or_summary": str(entry.get("quote_or_summary") or "")[:400],
            }
        )

    evidence = []
    for item in sections.get("evidence") or []:
        entry = _as_mapping(item)
        evidence.append(
            {
                "id": str(entry.get("id") or ""),
                "source_type": str(entry.get("source_type") or ""),
                "source_ref": str(entry.get("source_ref") or ""),
                "content": str(entry.get("content") or "")[:EVIDENCE_CHAR_LIMIT],
                "score": entry.get("score") if isinstance(entry.get("score"), (int, float)) else None,
                "retrieved_by": str(entry.get("retrieved_by") or ""),
            }
        )

    return {
        "final_answer": (str(payload.get("answer") or "") or None),
        "key_points": [str(point) for point in payload.get("key_points") or []],
        "citations": citations,
        "guardrail_flags": [_as_mapping(flag) for flag in payload.get("guardrail_flags") or []],
        "confidence": payload.get("confidence") if isinstance(
            payload.get("confidence"), (int, float)) else None,
        "evidence": evidence,
    }


def critic_of(sections: Mapping[str, Any]) -> tuple[bool | None, str | None]:
    critique = _as_mapping(sections.get("critique"))
    if not critique:
        return None, None
    approved = critique.get("approved")
    severity = critique.get("severity")
    return (bool(approved) if isinstance(approved, bool) else None, severity)


# --- Base record -----------------------------------------------------------


def base_record(
    entry: PlanEntry,
    item: EvalItem,
    config: ExperimentConfig,
    *,
    model: str,
    timeout_s: float,
    backend: str,
) -> dict[str, Any]:
    """Everything known before the run starts."""
    return {
        "run_key": entry.run_key,
        "prompt_id": item.id,
        "category": item.category,
        "difficulty": item.difficulty,
        "persona": item.persona,
        "pattern": entry.pattern,
        "context_sharing": entry.context_sharing,
        "seed": entry.seed,
        "repeat": entry.repeat,
        "tier": entry.tier,
        "backend": backend,
        "config": {
            "pattern": entry.pattern,
            "context_sharing": entry.context_sharing,
            "seed": entry.seed,
            "repeat": entry.repeat,
            "timeout_s": timeout_s,
            "tools": config.matrix.tool_selection,
            "llm_call_cap": config.matrix.llm_call_cap,
            "model": model,
            "backend": backend,
            "dataset_version": config.version,
            "harness_version": __version__,
        },
        "started_at": utc_now(),
    }


def merge_run_facts(
    record: dict[str, Any],
    *,
    run_id: str | None,
    summary: Mapping[str, Any] | None,
    sections: Mapping[str, Any],
    totals: Mapping[str, Any] | None,
    config: ExperimentConfig,
    model: str,
    latency_s: float,
    errors: list[str],
    notes: list[str],
) -> dict[str, Any]:
    """Fill a record from what the run actually produced."""
    summary = dict(summary or {})
    capture = capture_report(sections)
    approved, severity = critic_of(sections)

    tokens_in = totals.get("tokens_in") if totals else None
    tokens_out = totals.get("tokens_out") if totals else None
    if totals is None and summary.get("adk_tokens"):
        # Only the ADK side reported usage: input tokens are known, output is not.
        tokens_in = int(summary["adk_tokens"])
        notes.append("token counts come from the ADK event stream only")

    cost, basis = estimate_cost(config, model, tokens_in, tokens_out)

    stage_errors = list(summary.get("stage_errors") or [])
    failed = bool(summary.get("failed"))
    has_answer = bool((capture["final_answer"] or "").strip())
    if failed:
        terminal = "failed"
    elif has_answer:
        terminal = "completed"
    else:
        terminal = "failed"
        if not errors:
            errors = errors + ["no final report was written"]

    record.update(
        {
            "run_id": run_id,
            "ended_at": utc_now(),
            "latency_s": round(latency_s, 3),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "reasoning_tokens": totals.get("reasoning_tokens") if totals else None,
            "model_calls": totals.get("model_calls") if totals else None,
            "est_cost_usd": cost,
            "cost_basis": basis if cost is not None else (basis or "unknown_model"),
            "tool_calls": (
                summary.get("tool_calls")
                if summary.get("tool_calls") is not None
                else (totals.get("tool_calls") if totals else None)
            ),
            "recovery_rounds": summary.get("recovery_rounds"),
            "revision_rounds": summary.get("revision_rounds"),
            "evidence_count": len(capture["evidence"]),
            "terminal_state": terminal,
            "outcome": summary.get("outcome"),
            "critic_status": summary.get("critic_status"),
            "critic_approved": approved if approved is not None else summary.get("approved"),
            "confidence": capture["confidence"] if capture["confidence"] is not None
            else summary.get("confidence"),
            "final_answer": capture["final_answer"],
            "key_points": capture["key_points"],
            "citations": capture["citations"],
            "evidence": capture["evidence"],
            "guardrail_flags": capture["guardrail_flags"],
            "errors": errors,
            "stage_errors": stage_errors,
            "notes": notes,
        }
    )
    if severity:
        record["notes"].append(f"critic severity: {severity}")
    return record


# --- Executors -------------------------------------------------------------


class ExecutorError(RuntimeError):
    """A precondition of the executor failed; the batch should stop."""


@dataclass
class ExecutorSpec:
    """Everything an executor needs that is not in the plan entry."""

    config: ExperimentConfig
    dataset: EvalDataset
    timeout_s: float
    log_dir: Path
    model: str


class InProcessExecutor:
    """Calls ``athena.run.run_one`` in this process (the preferred path)."""

    backend = "in_process"

    def __init__(self, spec: ExecutorSpec) -> None:
        self.spec = spec

    def __call__(self, entry: PlanEntry, item: EvalItem) -> dict[str, Any]:
        from athena.run import run_one  # imported here so --dry-run needs no LLM stack

        record = base_record(
            entry, item, self.spec.config,
            model=self.spec.model, timeout_s=self.spec.timeout_s, backend=self.backend,
        )
        errors: list[str] = []
        notes: list[str] = []
        log_path = self.spec.log_dir / f"{entry.run_key}.log"
        run_id: str | None = None
        summary: dict[str, Any] = {}
        started = time.perf_counter()

        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                try:
                    result = asyncio.run(
                        asyncio.wait_for(run_one(item.prompt, entry.pattern), self.spec.timeout_s)
                    )
                    summary = _as_mapping(result)
                    run_id = summary.get("run_id") or None
                except (asyncio.TimeoutError, TimeoutError):
                    elapsed = time.perf_counter() - started
                    errors.append(f"timeout after {self.spec.timeout_s:.0f}s")
                    notes.append("run was cancelled; its blackboard state may be partial")
                    record.update(
                        {
                            "ended_at": utc_now(),
                            "latency_s": round(elapsed, 3),
                            "terminal_state": "timeout",
                            "errors": errors,
                            "notes": notes,
                            "log_path": _relative(log_path),
                            "final_answer": None,
                        }
                    )
                    return RunRecord.model_validate(record).model_dump(mode="json")
                except Exception as exc:  # noqa: BLE001 - one bad run must not end the batch
                    elapsed = time.perf_counter() - started
                    errors.append(f"{type(exc).__name__}: {exc}")
                    notes.append("the harness caught this; the batch continued")
                    record.update(
                        {
                            "ended_at": utc_now(),
                            "latency_s": round(elapsed, 3),
                            "terminal_state": "error",
                            "errors": errors,
                            "notes": notes,
                            "log_path": _relative(log_path),
                            "final_answer": None,
                        }
                    )
                    return RunRecord.model_validate(record).model_dump(mode="json")

        elapsed = time.perf_counter() - started
        sections = blackboard_sections(run_id or entry.run_key)
        totals = event_totals(run_id or entry.run_key)
        record = merge_run_facts(
            record, run_id=run_id, summary=summary, sections=sections, totals=totals,
            config=self.spec.config, model=self.spec.model, latency_s=elapsed,
            errors=errors, notes=notes,
        )
        record["log_path"] = _relative(log_path)
        return RunRecord.model_validate(record).model_dump(mode="json")


class SubprocessExecutor:
    """Runs ``python -m athena.run`` as a child process.

    This is the same entry point the demo HTTP API uses, and it is the fallback
    when the in-process path cannot be imported. A timeout really kills the
    child, so it is the honest way to enforce ``per_run_timeout_s``.
    """

    backend = "subprocess"

    def __init__(self, spec: ExecutorSpec) -> None:
        self.spec = spec

    def __call__(self, entry: PlanEntry, item: EvalItem) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        record = base_record(
            entry, item, self.spec.config,
            model=self.spec.model, timeout_s=self.spec.timeout_s, backend=self.backend,
        )
        errors: list[str] = []
        notes: list[str] = []
        log_path = self.spec.log_dir / f"{entry.run_key}.log"
        summary_path = self.spec.log_dir / f"{entry.run_key}.summary.json"
        argv = [
            sys.executable, "-m", "athena.run", item.prompt,
            "--pattern", entry.pattern, "--run-id", run_id,
            "--json-summary", str(summary_path),
        ]
        started = time.perf_counter()
        timed_out = False
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                argv, cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT,
                env=dict(os.environ),
            )
            try:
                process.wait(timeout=self.spec.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.wait(timeout=30)
        elapsed = time.perf_counter() - started

        if timed_out:
            record.update(
                {
                    "run_id": run_id,
                    "ended_at": utc_now(),
                    "latency_s": round(elapsed, 3),
                    "terminal_state": "timeout",
                    "errors": [f"timeout after {self.spec.timeout_s:.0f}s"],
                    "notes": ["child process killed"],
                    "log_path": _relative(log_path),
                    "final_answer": None,
                }
            )
            return RunRecord.model_validate(record).model_dump(mode="json")

        summary: dict[str, Any] = {}
        # The batch writes its own copy; the demo API's location is read-only
        # fallback for a run that was started by something else.
        for candidate in (summary_path, RUN_SUMMARY_DIR / f"{run_id}.summary.json"):
            if not candidate.exists():
                continue
            try:
                summary = json.loads(candidate.read_text(encoding="utf-8"))
            except ValueError:
                errors.append(f"summary JSON at {candidate} was unreadable")
                continue
            break
        else:
            errors.append(f"child exited {process.returncode} without writing a summary")

        sections = blackboard_sections(run_id)
        totals = event_totals(run_id)
        record = merge_run_facts(
            record, run_id=run_id, summary=summary, sections=sections, totals=totals,
            config=self.spec.config, model=self.spec.model, latency_s=elapsed,
            errors=errors, notes=notes,
        )
        record["log_path"] = _relative(log_path)
        return RunRecord.model_validate(record).model_dump(mode="json")


def build_executor(spec: ExecutorSpec, backend: str):
    if backend == "subprocess":
        return SubprocessExecutor(spec)
    return InProcessExecutor(spec)


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except Exception:  # noqa: BLE001 - outside the repo is fine, just absolute
        return str(path)


# --- Batch -----------------------------------------------------------------


def read_records(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read ``raw.jsonl`` leniently: a torn last line is reported, not fatal."""
    records: list[dict[str, Any]] = []
    problems: list[str] = []
    if not path.exists():
        return records, problems
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            records.append(json.loads(text))
        except ValueError:
            problems.append(f"raw.jsonl line {number} is not valid JSON")
    return records, problems


def done_keys(records: Iterable[Mapping[str, Any]], retry_failed: bool = False) -> set[str]:
    """Run keys that must be skipped on resume."""
    keys: set[str] = set()
    for record in records:
        key = str(record.get("run_key") or "")
        if not key:
            continue
        if retry_failed and record.get("terminal_state") not in {"completed", "failed"}:
            continue
        keys.add(key)
    return keys


class BudgetTracker:
    """Stops the batch instead of letting it spend without limit."""

    def __init__(self, budget: schema.Budget) -> None:
        self.budget = budget
        self.spent_usd = 0.0
        self.runs = 0
        self.started = time.monotonic()

    def exhausted(self) -> str | None:
        if self.runs >= self.budget.max_runs:
            return f"max_runs={self.budget.max_runs} reached"
        if self.spent_usd >= self.budget.max_total_usd:
            return f"max_total_usd={self.budget.max_total_usd} reached"
        if time.monotonic() - self.started >= self.budget.max_total_wall_s:
            return f"max_total_wall_s={self.budget.max_total_wall_s:.0f} reached"
        return None

    def record(self, record: Mapping[str, Any]) -> str | None:
        self.runs += 1
        cost = record.get("est_cost_usd")
        if isinstance(cost, (int, float)):
            self.spent_usd += float(cost)
        if isinstance(cost, (int, float)) and cost > self.budget.max_run_usd:
            return (
                f"run cost estimate {cost:.4f} USD exceeded max_run_usd="
                f"{self.budget.max_run_usd}"
            )
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs_started": self.runs,
            "spent_usd_estimate": round(self.spent_usd, 6),
            "max_runs": self.budget.max_runs,
            "max_total_usd": self.budget.max_total_usd,
            "max_run_usd": self.budget.max_run_usd,
            "max_total_wall_s": self.budget.max_total_wall_s,
        }


class Runner:
    """Executes the selected plan entries and appends one record per attempt."""

    def __init__(
        self,
        dataset: EvalDataset,
        config: ExperimentConfig,
        out_dir: Path,
        *,
        entries: Sequence[PlanEntry] | None = None,
        executor: Callable[[PlanEntry, EvalItem], dict[str, Any]] | None = None,
        backend: str | None = None,
        timeout_s: float | None = None,
        resume: bool = True,
        retry_failed: bool = False,
        limit: int | None = None,
        model: str | None = None,
        max_runs: int | None = None,
        max_usd: float | None = None,
        config_path: Path | None = None,
        dataset_path: Path | None = None,
        logger: Callable[[str], None] = print,
    ) -> None:
        self.dataset = dataset
        self.config = config
        self.out_dir = out_dir
        self.backend = (backend or config.matrix.backend)
        self.timeout_s = float(timeout_s or config.matrix.per_run_timeout_s)
        self.resume = resume
        self.retry_failed = retry_failed
        self.limit = limit
        self.model = model or (os.getenv("LLM_MODEL") or "default")
        self.logger = logger
        self.entries = list(entries) if entries is not None else list(config.entries())
        self.config_path = config_path or DEFAULT_CONFIG
        self.dataset_path = dataset_path or DEFAULT_DATASET

        budget = config.budget.model_copy(
            update={
                "max_runs": max_runs if max_runs is not None else config.budget.max_runs,
                "max_total_usd": max_usd if max_usd is not None else config.budget.max_total_usd,
            }
        )
        self.budget = BudgetTracker(budget)

        if executor is None:
            spec = ExecutorSpec(
                config=config, dataset=dataset, timeout_s=self.timeout_s,
                log_dir=out_dir / "logs", model=self.model,
            )
            self.executor = build_executor(spec, self.backend)
        else:
            self.executor = executor

        self.raw_path = out_dir / "raw.jsonl"
        self.records: list[dict[str, Any]] = []

    # --- selection ------------------------------------------------------
    def selected(self) -> list[PlanEntry]:
        entries = list(self.entries)
        if self.limit is not None:
            entries = entries[: self.limit]
        return entries

    def pending(self, entries: Sequence[PlanEntry]) -> tuple[list[PlanEntry], set[str]]:
        existing, problems = read_records(self.raw_path)
        for problem in problems:
            self.logger(f"warning: {problem}")
        skipped = done_keys(existing, self.retry_failed) if self.resume else set()
        return [entry for entry in entries if entry.run_key not in skipped], skipped

    # --- execution ------------------------------------------------------
    def run(self) -> dict[str, Any]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "logs").mkdir(exist_ok=True)

        entries = self.selected()
        pending, skipped = self.pending(entries)
        self.logger(
            f"plan {self.config.plan.id}: {len(entries)} entries, "
            f"{len(skipped)} already recorded, {len(pending)} to run"
        )

        meta = {
            "harness_version": __version__,
            "started_at": utc_now(),
            "git_commit": git_commit(),
            "python": sys.version.split()[0],
            "backend": self.backend,
            "model": self.model,
            "timeout_s": self.timeout_s,
            "dataset_path": str(self.config.dataset_path),
            "dataset_sha256": _digest(self.dataset_path),
            "config_sha256": _digest(self.config_path),
            "dataset_items": len(self.dataset.items),
            "plan_id": self.config.plan.id,
            "plan_entries": len(entries),
            "already_recorded": sorted(skipped),
            "pricing": pricing_status(),
            "budget": self.budget.as_dict(),
            "stopped_reason": None,
            "records_written": 0,
        }
        (self.out_dir / "run_meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )

        stopped_reason: str | None = None
        remaining: list[PlanEntry] = []
        for index, entry in enumerate(pending, start=1):
            reason = self.budget.exhausted()
            if reason:
                stopped_reason = reason
                remaining = pending[index - 1:]
                self.logger(f"stopping: {reason}")
                break

            item = self.dataset.by_id(entry.prompt_id)
            self.logger(
                f"[{index}/{len(pending)}] {entry.run_key} ({item.category}, "
                f"{item.difficulty})"
            )
            try:
                record = self.executor(entry, item)
            except KeyboardInterrupt:
                stopped_reason = "interrupted"
                remaining = pending[index - 1:]
                raise
            except Exception as exc:  # noqa: BLE001 - never lose the batch to one run
                record = self._harness_failure(entry, item, exc)

            self._append(record)
            self.records.append(record)
            over_budget = self.budget.record(record)
            self.logger(
                f"    -> {record.get('terminal_state')} "
                f"{record.get('latency_s')}s "
                f"{(record.get('est_cost_usd') if record.get('est_cost_usd') is not None else 'cost n/a')}"
            )
            if over_budget:
                stopped_reason = over_budget
                remaining = pending[index:]
                self.logger(f"stopping: {over_budget}")
                break

        meta.update(
            {
                "ended_at": utc_now(),
                "records_written": len(self.records),
                "budget": self.budget.as_dict(),
                "stopped_reason": stopped_reason,
            }
        )
        (self.out_dir / "run_meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
        (self.out_dir / "plan.json").write_text(
            json.dumps(
                {
                    "plan_id": self.config.plan.id,
                    "entries": [entry.model_dump(mode="json") for entry in entries],
                    "not_run": [entry.model_dump(mode="json") for entry in remaining],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        summary = {
            "out_dir": str(self.out_dir),
            "records": len(self.records),
            "skipped": len(skipped),
            "stopped_reason": stopped_reason,
            "budget": self.budget.as_dict(),
            "states": _count_states(self.records),
        }
        self.logger(json.dumps(summary, indent=2))
        return summary

    def _harness_failure(
        self, entry: PlanEntry, item: EvalItem, exc: Exception
    ) -> dict[str, Any]:
        """A failure raised outside the run itself is recorded, never hidden."""
        record = base_record(
            entry, item, self.config,
            model=self.model, timeout_s=self.timeout_s, backend=self.backend,
        )
        record.update(
            {
                "ended_at": utc_now(),
                "latency_s": 0.0,
                "terminal_state": "error",
                "errors": [f"{type(exc).__name__}: {exc}"],
                "notes": ["the harness failed before a run was executed"],
                "final_answer": None,
            }
        )
        return RunRecord.model_validate(record).model_dump(mode="json")

    def _append(self, record: Mapping[str, Any]) -> None:
        with self.raw_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _count_states(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        state = str(record.get("terminal_state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return counts


# --- Preconditions ---------------------------------------------------------


#: Keep this in step with ``athena.agents.a2a_stage.DEFAULT_CARD_URL``; it is
#: repeated here so ``--dry-run`` does not import the ADK stack just to probe a
#: loopback port.
CARD_URL = "http://127.0.0.1:9001/.well-known/agent-card.json"


def load_env() -> str:
    """Load ``.env`` exactly the way the pipeline does.

    ``athena.config.llm`` calls ``load_dotenv()`` on import, so importing it is
    the supported way to get the same environment a real run sees. A failure is
    reported, never fatal.
    """
    try:
        import athena.config.llm  # noqa: F401  (import side effect loads .env)

        return "loaded"
    except Exception as exc:  # noqa: BLE001 - a batch may still be dry-runnable
        return f"unavailable ({type(exc).__name__})"


def preflight(*, timeout_s: float = 2.0) -> dict[str, Any]:
    """Report the conditions a live batch needs. Never raises, never calls a model."""
    status: dict[str, Any] = {
        "env_file": load_env(),
        "crew_a2a_server": "unknown",
        "llm_api_key": "present" if os.getenv("LLM_API_KEY") else "absent",
        "model": os.getenv("LLM_MODEL") or "default",
        "chroma_path": os.getenv("CHROMA_PATH") or "data/chroma",
        "blackboard_db": os.getenv("BLACKBOARD_DB") or "data/blackboard.db",
    }
    try:
        import httpx

        response = httpx.get(CARD_URL, timeout=timeout_s)
        status["crew_a2a_server"] = (
            "up" if response.status_code == 200 else f"http {response.status_code}"
        )
    except Exception as exc:  # noqa: BLE001 - "down" is information, not a crash
        status["crew_a2a_server"] = f"down ({type(exc).__name__})"
    return status


# --- CLI -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.runner",
        description="Run the Athena evaluation plan and record one JSONL line per run.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.json")
    parser.add_argument(
        "--plan", default="",
        help="alias for --config: the file holding the matrix, caps and committed plan",
    )
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="path to dataset.json")
    parser.add_argument(
        "--out", default="",
        help="results directory (default evaluation/results/<UTC timestamp>)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and print the plan; execute nothing, write nothing")
    parser.add_argument("--resume", dest="resume", action="store_true", default=True,
                        help="skip run keys already in raw.jsonl (default)")
    parser.add_argument("--no-resume", dest="resume", action="store_false",
                        help="re-run every selected entry")
    parser.add_argument("--retry-failed", action="store_true",
                        help="with --resume, re-run keys whose state is not completed/failed")
    parser.add_argument("--limit", type=int, default=None, help="run at most N entries")
    parser.add_argument("--only", default="", help="comma-separated prompt ids")
    parser.add_argument("--run-key", default="", help="comma-separated run keys")
    parser.add_argument("--pattern", default="", help="comma-separated patterns")
    parser.add_argument("--tier", default="", help="comma-separated tiers")
    parser.add_argument("--timeout", type=float, default=None, help="per-run timeout seconds")
    parser.add_argument("--backend", default=None, choices=list(schema.BACKENDS))
    parser.add_argument("--max-runs", type=int, default=None)
    parser.add_argument("--max-usd", type=float, default=None)
    parser.add_argument("--model", default=None,
                        help="model label to price against (default LLM_MODEL)")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="print the dry-run/plan summary as JSON")
    return parser


def _split(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def select_entries(
    entries: Sequence[PlanEntry],
    *,
    prompts: Sequence[str] = (),
    run_keys: Sequence[str] = (),
    patterns: Sequence[str] = (),
    tiers: Sequence[str] = (),
) -> list[PlanEntry]:
    """Filter the plan without ever inventing an entry."""
    selected = list(entries)
    if prompts:
        wanted = set(prompts)
        selected = [e for e in selected if e.prompt_id in wanted]
    if run_keys:
        wanted_keys = set(run_keys)
        selected = [e for e in selected if e.run_key in wanted_keys]
    if patterns:
        wanted_patterns = set(patterns)
        selected = [e for e in selected if e.pattern in wanted_patterns]
    if tiers:
        wanted_tiers = set(tiers)
        selected = [e for e in selected if e.tier in wanted_tiers]
    return selected


def describe_plan(
    dataset: EvalDataset, config: ExperimentConfig, entries: Sequence[PlanEntry],
    status: Mapping[str, Any], *, timeout_s: float, backend: str, model: str,
) -> dict[str, Any]:
    """The dry-run payload: what would run, and whether it could."""
    by_pattern: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for entry in entries:
        by_pattern[entry.pattern] = by_pattern.get(entry.pattern, 0) + 1
        by_tier[entry.tier] = by_tier.get(entry.tier, 0) + 1
        item = dataset.by_id(entry.prompt_id)
        by_category[item.category] = by_category.get(item.category, 0) + 1

    prompts = sorted({entry.prompt_id for entry in entries})
    return {
        "dry_run": True,
        "harness_version": __version__,
        "dataset": {
            "path": config.dataset_path,
            "items": len(dataset.items),
            "categories": dataset.category_counts(),
            "personas": dataset.persona_counts(),
            "difficulty": dataset.difficulty_counts(),
        },
        "plan": {
            "id": config.plan.id,
            "committed_entries": len(config.entries()),
            "selected_entries": len(entries),
            "distinct_prompts": len(prompts),
            "by_pattern": by_pattern,
            "by_tier": by_tier,
            "by_category": by_category,
            "first_entries": [entry.run_key for entry in entries[:5]],
        },
        "run": {
            "backend": backend,
            "model": model,
            "per_run_timeout_s": timeout_s,
            "pricing_note": config.pricing.note,
            "pricing": pricing_status(),
            "pricing_summary": pricing_summary(config, model),
        },
        "budget": config.budget.model_dump(mode="json"),
        "preconditions": dict(status),
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)

    # Get the same environment a real run sees before anything reads LLM_MODEL.
    load_env()

    config_path = Path(args.plan or args.config)
    dataset_path = Path(args.dataset)
    try:
        config = schema.load_config(config_path)
        dataset = schema.load_dataset(dataset_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except Exception as exc:  # noqa: BLE001 - pydantic validation detail is the message
        print(f"error: configuration is not valid: {exc}", file=sys.stderr)
        return EXIT_INVALID

    entries = select_entries(
        config.entries(),
        prompts=_split(args.only),
        run_keys=_split(args.run_key),
        patterns=_split(args.pattern),
        tiers=_split(args.tier),
    )
    if args.limit is not None:
        entries = entries[: args.limit]
    if not entries:
        print("error: the filters select no plan entries", file=sys.stderr)
        return EXIT_INVALID

    unknown = sorted({e.prompt_id for e in entries} - {item.id for item in dataset.items})
    if unknown:
        print(f"error: plan references unknown prompt ids {unknown}", file=sys.stderr)
        return EXIT_INVALID

    timeout_s = float(args.timeout or config.matrix.per_run_timeout_s)
    backend = args.backend or config.matrix.backend
    model = args.model or (os.getenv("LLM_MODEL") or "default")

    if args.dry_run:
        status = preflight(timeout_s=1.0)
        payload = describe_plan(
            dataset, config, entries, status,
            timeout_s=timeout_s, backend=backend, model=model,
        )
        if args.json_out:
            print(json.dumps(payload, indent=2))
            return EXIT_OK
        print("DRY RUN - nothing was executed and no files were written\n")
        print(f"dataset   : {payload['dataset']['items']} items, "
              f"{len(payload['dataset']['categories'])} categories")
        print(f"plan      : {payload['plan']['id']} - {payload['plan']['selected_entries']} "
              f"runs from {payload['plan']['distinct_prompts']} prompts")
        print("patterns  : " + ", ".join(
            f"{name}={count}" for name, count in sorted(payload["plan"]["by_pattern"].items())
        ))
        print("tiers     : " + ", ".join(
            f"{name}={count}" for name, count in sorted(payload["plan"]["by_tier"].items())
        ))
        print("run       : backend=" + backend + f" model={model} timeout={timeout_s:.0f}s")
        print(f"budget    : <= {config.budget.max_runs} runs, "
              f"<= {config.budget.max_total_usd} USD, <= {config.budget.max_run_usd} USD/run")
        price = payload["run"].get("pricing_summary") or {}
        print(f"pricing   : {price.get('basis', 'unknown')}")
        print("first 5   : " + ", ".join(payload['plan']['first_entries']))
        print("\npreconditions (a live batch needs both):")
        for name, value in status.items():
            print(f"  {name:<18} {value}")
        if status.get("crew_a2a_server") != "up" or status.get("llm_api_key") != "present":
            print("\nnote: preconditions are not satisfied. That is fine for a dry run; "
                  "a live batch will refuse to start.")
        print("\nno results were written; rerun without --dry-run to execute.")
        return EXIT_OK

    # --- live batch -----------------------------------------------------
    out_dir = Path(args.out) if args.out else DEFAULT_RESULTS_ROOT / timestamp_slug()
    status = preflight()
    if status.get("crew_a2a_server") != "up":
        print(
            "error: the CrewAI A2A server is not answering "
            f"({status.get('crew_a2a_server')}).\n"
            "Start it with:  python scripts/dev_up.py",
            file=sys.stderr,
        )
        return EXIT_PRECONDITION
    if status.get("llm_api_key") != "present":
        print("error: LLM_API_KEY is not set; no run can be made.", file=sys.stderr)
        return EXIT_PRECONDITION

    price = pricing_summary(config, model)
    if not price["priced"]:
        print(
            f"warning: model {model!r} has no rate in evaluation/config.json pricing or "
            "data/pricing.json. Every run will record cost null and the cost charts will be "
            "empty. Add a rate for it (or run a model that is listed) if cost matters.",
            file=sys.stderr,
        )

    runner = Runner(
        dataset, config, out_dir,
        entries=entries,
        backend=backend,
        timeout_s=timeout_s,
        resume=args.resume,
        retry_failed=args.retry_failed,
        limit=None,
        model=model,
        max_runs=args.max_runs,
        max_usd=args.max_usd,
        config_path=config_path,
        dataset_path=dataset_path,
    )

    try:
        runner.run()
    except KeyboardInterrupt:
        print("\ninterrupted: records written so far are kept; rerun to resume.",
              file=sys.stderr)
        return EXIT_INTERRUPTED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
