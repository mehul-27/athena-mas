"""Run the evaluation plan and record exactly what happened.

    python -m evaluation.runner --dry-run
    python -m evaluation.runner --out evaluation/results/20250101T000000Z
    python -m evaluation.runner --plan evaluation/config.json --out evaluation/results/<ts>

Design rules this file follows, because an evaluation harness is worthless if it
can quietly lie:

* One JSONL line per **real attempt**. A run that did not happen produces no
  record; a run that failed produces a record saying so.
* An attempt is identified by ``attempt_id`` and numbered ``attempt``. A retried
  run appends a *second* record with a new id; the first attempt is never
  rewritten, so the file is an append-only audit trail.
* Only transient infrastructure failures are retried. A critic rejection, thin
  evidence, a timeout or a completed run is a result and is never re-rolled to
  get a nicer sample.
* Nothing is estimated into existence. Token counts come from the event store,
  cost comes from a published price table or stays ``null`` (with
  ``pricing.priced = False`` naming the unpriced model), and the final answer
  comes from the blackboard ``final_report``.
* Provenance is resolved from the *same* helper ``scripts/dev_up.py`` uses, and
  the ADK and CrewAI models are recorded separately because they can differ.
* A timeout or an exception is recorded and the batch continues.
* The batch resumes: keys already present in ``raw.jsonl`` are skipped, so an
  interrupted batch does not start over and does not double-count.
* ``--dry-run`` writes nothing at all.

The preferred execution path is in-process (``athena.run.run_one``). The
``subprocess`` backend exists because that is exactly what the demo HTTP API does
(``athena/api/runs.py`` spawns ``python -m athena.run``), so the fallback is the
same code path the web UI uses.

.. warning::
   For a **live** batch use ``--backend subprocess``. The in-process executor
   redirects this process's stdout/stderr into a per-run log file; the MCP stdio
   toolsets the agents spawn then inherit a closed stream and die with
   ``ValueError: I/O operation on closed file``, which is why
   ``results/20261106T194245Z`` recorded zero evidence for every run.
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from evaluation import __version__, outcomes, schema
from evaluation import accounts as accounts_module
from evaluation.schema import (
    AttemptAccounting,
    EvalDataset,
    EvalItem,
    ExperimentConfig,
    ModelProvenance,
    PlanEntry,
    PricingRecord,
    RunRecord,
)

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

#: One run key may be executed at most this many times (first attempt + one
#: retry) and only for a transient infrastructure failure. The batch-wide cap is
#: ``budget.max_runs`` (40 in the focused plan), which counts *attempts*.
DEFAULT_MAX_ATTEMPTS_PER_RUN = 2

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


# --- Provenance (B1) -------------------------------------------------------


def redact_host(url: str) -> str:
    """An endpoint with credentials, query and fragment removed.

    Reuses :func:`athena.tools.diagnostics.redact_url`, which is the repo's single
    redaction implementation, so the harness cannot invent a weaker one.
    """
    value = (url or "").strip()
    if not value:
        return ""
    try:
        from athena.tools.diagnostics import redact_url

        return redact_url(value)
    except Exception:  # noqa: BLE001 - provenance is reported, never fatal
        return "(unredactable url; suppressed)"


def load_accounts(names: Sequence[str]) -> tuple[Any, ...]:
    """Resolve named settings profiles to authorised accounts.

    Reads the key from the existing encrypted vault - the same store the
    Settings UI writes - so there is one credential source, not a second one.
    The key is carried only into the child's environment; it is never logged,
    recorded or returned in a description.

    Raises :class:`evaluation.accounts.AccountError` when a profile is missing,
    has no key, or when the set would not run comparable science.
    """
    if not names:
        return ()
    from athena.settings import profiles as profile_store
    from athena.settings.vault import default_vault

    vault = default_vault()
    found = []
    for name in names:
        label = name.strip()
        if not label:
            continue
        profile = next(
            (p for p in profile_store.load_profiles() if p.name == label), None
        )
        if profile is None:
            raise accounts_module.AccountError(f"no settings profile named {label!r}")
        key = vault.get(profile_store.key_name(profile.id, "llm"))
        if not key:
            raise accounts_module.AccountError(
                f"profile {label!r} has no stored API key"
            )
        found.append(
            accounts_module.Account(
                label=label,
                key=key,
                api_base=profile.api_base,
                model=profile.model,
            )
        )
    accounts_module.verify_same_conditions(found)
    return tuple(found)


def active_profile_env() -> tuple[dict[str, str], str]:
    """The active settings profile's env overrides, via the same helper dev_up uses.

    ``scripts/dev_up.py`` does ``from athena.api.activation import active_env`` and
    merges the result into the server's environment. That is the *only* profile
    resolution in the repo and this reuses it rather than reimplementing one: a
    second copy would be a second answer to "which model ran?".

    Returns ``(env, name)`` with ``name`` a human label for where it came from.
    Never contains anything the caller may print.
    """
    try:
        from athena.api.activation import active_env

        env = dict(active_env() or {})
        return env, "settings_profile" if env else "none"
    except Exception as exc:  # noqa: BLE001 - a bare checkout has no settings
        return {}, f"unavailable ({type(exc).__name__})"


def _provider_of(model: str) -> str:
    return (model or "").split("/", 1)[0].strip().lower()


def resolve_provenance(
    *,
    env: Mapping[str, str] | None = None,
    profile: Mapping[str, str] | None = None,
) -> ModelProvenance:
    """Which model each half of the pipeline will actually call.

    The ADK stages run in *this* process (or in the ``python -m athena.run`` child
    it spawns), so they read this process's environment. The CrewAI stages run in
    the server started by ``scripts/dev_up.py``, whose environment is the active
    settings profile merged over the shell environment. Recording one ``model``
    for both is bug B1; this records each separately, plus the redacted endpoint
    and whether a profile was applied.

    Keys are never read into the result, printed, or persisted.
    """
    base = dict(os.environ if env is None else env)
    active = dict(active_profile_env()[0] if profile is None else profile)

    adk_model = (base.get("LLM_MODEL") or "").strip()
    crew_model = (active.get("LLM_MODEL") or adk_model).strip()
    adk_host = redact_host(base.get("LLM_API_BASE") or "")
    crew_host = redact_host(active.get("LLM_API_BASE") or base.get("LLM_API_BASE") or "")

    profile_id = profile_name = ""
    if active:
        try:
            from athena.settings import profiles as profile_store

            profile_id = profile_store.active_id()
            found = profile_store.find(profile_id) if profile_id else None
            profile_name = (found.name if found else "") or ""
        except Exception:  # noqa: BLE001 - a name is a nicety, not a requirement
            profile_id = profile_id or ""

    provenance = ModelProvenance(
        adk_model=adk_model,
        crew_model=crew_model,
        provider=_provider_of(adk_model),
        crew_provider=_provider_of(crew_model),
        api_host=adk_host,
        crew_api_host=crew_host,
        profile_applied=bool(active),
        profile_id=profile_id,
        profile_name=profile_name,
        env_source="profile" if active else "env",
        models_differ=bool(adk_model and crew_model and adk_model != crew_model),
        note=(
            "ADK stages (planner/researchers) read this process's environment; the "
            "CrewAI stages (analyst/writer/critic) run in the dev_up.py server, whose "
            "environment is the active settings profile merged over it."
            if active
            else "No settings profile is active; both halves read this process's "
            "environment, so the models should agree."
        ),
    )
    return provenance


# --- Output directory (B4) -------------------------------------------------


class OutputDirectoryError(RuntimeError):
    """The results directory cannot be created or written to."""


def prepare_out_dir(out_dir: Path) -> Path:
    """Create the results/log directory, or fail with one readable message.

    B4: ``--out`` into a path Python cannot create used to escape as a raw
    ``PermissionError`` traceback with no record and no ``run_meta.json``. This
    turns it into a clean :class:`OutputDirectoryError`, which ``main`` reports and
    maps to ``EXIT_INVALID``; nothing is written in that case, not even a partial
    ``run_meta.json``.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "logs").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputDirectoryError(
            f"cannot create the results directory {out_dir}: {exc}"
        ) from exc
    probe = out_dir / ".write-probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise OutputDirectoryError(
            f"the results directory {out_dir} is not writable: {exc}"
        ) from exc
    return out_dir


def discard_empty_out_dir(out_dir: Path) -> None:
    """Remove a results directory this process created and left empty.

    A batch refused by a later gate should not leave an empty ``results/<ts>``
    behind pointing at a run that never happened. Only an empty tree is removed:
    anything holding a record, log or ``run_meta.json`` is kept, so a pre-existing
    batch can never be deleted by this.
    """
    try:
        logs = out_dir / "logs"
        if logs.is_dir() and not any(logs.iterdir()):
            logs.rmdir()
        if out_dir.is_dir() and not any(out_dir.iterdir()):
            out_dir.rmdir()
    except OSError:  # noqa: BLE001 - tidying is best-effort, never fatal
        pass


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


def unpriced_models(config: ExperimentConfig, *models: str) -> list[str]:
    """The distinct models among ``models`` that no price table can price."""
    unpriced: list[str] = []
    for name in models:
        key = (name or "").strip()
        if not key or key in unpriced:
            continue
        if not pricing_summary(config, key)["priced"]:
            unpriced.append(key)
    return unpriced


def usd_cap_is_enforceable(config: ExperimentConfig, *models: str) -> bool:
    """Can ``max_total_usd``/``max_run_usd`` actually bound spend for these models?

    Only if every model is priced. Summing null costs yields zero, so an unpriced
    batch hits the *run-count* cap and never the dollar cap: the USD limits are
    inert, not protective, and the runner says so rather than implying safety.
    """
    return not unpriced_models(config, *models)


def estimate_cost(
    config: ExperimentConfig,
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
) -> tuple[float | None, str]:
    """USD estimate for one run, or ``(None, reason)`` when it cannot be priced.

    ``config.json -> pricing`` is consulted first because it is committed with the
    plan; the repo's bundled table (``athena.observability.cost``) is the
    fallback. A model in neither table stays unpriced: no rate is interpolated and
    no neighbour's rate is borrowed.
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


def pricing_record(
    config: ExperimentConfig, model: str, cost: float | None, basis: str
) -> PricingRecord:
    """The record-level answer to "is this cost a number or an unknown?"."""
    if cost is not None:
        return PricingRecord(
            priced=True, basis=basis, currency=config.pricing.currency
        )
    named = (model or "").strip() or "(no model configured)"
    return PricingRecord(
        priced=False,
        unpriced_models=[named],
        basis=(
            f"unpriced:{named} has no rate in evaluation/config.json pricing or "
            "data/pricing.json; est_cost_usd is null, not zero"
        ),
        currency=config.pricing.currency,
    )


def pricing_status_for_meta(config: ExperimentConfig, *models: str) -> dict[str, Any]:
    """``run_meta["pricing_status"]``: what is priced, what is not, and what that means."""
    missing = unpriced_models(config, *models)
    return {
        "models": [name for name in models if name],
        "unpriced_models": missing,
        "priced": not missing,
        "usd_cap_enforceable": not missing,
        "note": (
            "Every run records est_cost_usd=null and pricing.priced=false for "
            f"{missing}. The USD caps (max_total_usd={config.budget.max_total_usd}, "
            f"max_run_usd={config.budget.max_run_usd}) therefore cannot bound real "
            "spend for this batch - they are inert, and max_runs="
            f"{config.budget.max_runs} is the only hard cap. Do not read a zero "
            "total as 'nothing was spent'."
            if missing
            else (
                "All batch models are priced; cost estimates and the USD caps are "
                "meaningful (as estimates, not invoices)."
            )
        ),
    }


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


def new_attempt_id(run_key: str, attempt: int) -> str:
    """A value that is unique per *execution*, not per run key.

    ``uuid4`` is the uniqueness source; the readable prefix makes a retry visible
    in ``raw.jsonl`` without having to join on ``run_key``.
    """
    return f"{run_key}#a{attempt}-{uuid.uuid4().hex[:12]}"


def base_record(
    entry: PlanEntry,
    item: EvalItem,
    config: ExperimentConfig,
    *,
    model: str,
    timeout_s: float,
    backend: str,
    attempt: int = 1,
    attempt_id: str = "",
    provenance: ModelProvenance | None = None,
) -> dict[str, Any]:
    """Everything known before the run starts."""
    resolved = provenance or ModelProvenance(adk_model=model)
    return {
        "run_key": entry.run_key,
        "attempt_id": attempt_id or new_attempt_id(entry.run_key, attempt),
        "attempt": attempt,
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
        "provenance": resolved.model_dump(mode="json"),
        "config": {
            "pattern": entry.pattern,
            "context_sharing": entry.context_sharing,
            "seed": entry.seed,
            "repeat": entry.repeat,
            "timeout_s": timeout_s,
            "tools": config.matrix.tool_selection,
            "llm_call_cap": config.matrix.llm_call_cap,
            "model": model,
            "adk_model": resolved.adk_model,
            "crew_model": resolved.crew_model,
            "provider": resolved.provider,
            "api_host": resolved.api_host,
            "profile_applied": resolved.profile_applied,
            "env_source": resolved.env_source,
            "seed_propagated_to_provider": schema.SEED_PROPAGATED_TO_PROVIDER,
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

    # Decide the terminal state from the *cause*, not from the fact that a
    # summary exists. The old rule only reached `infra_error` when the child
    # wrote no summary at all, so a run killed by an exhausted provider quota
    # still reported a summary with failed=True and was recorded as an ordinary
    # pipeline failure - which made an availability outage look like a research
    # verdict. `outcomes.classify` reads the recorded error text and names the
    # cause; only a genuine infrastructure cause becomes `infra_error`.
    provisional = {
        "terminal_state": "completed" if has_answer and not failed else "failed",
        "final_answer": capture["final_answer"],
        "critic_approved": approved,
        "model_calls": totals.get("model_calls") if totals else None,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "errors": list(errors),
        "stage_errors": stage_errors,
        "notes": list(notes),
    }
    classification = outcomes.classify(provisional)

    if has_answer and not failed:
        terminal = "completed"
    elif classification.is_infrastructure:
        terminal = "infra_error"
    elif classification.failure_class == "budget":
        terminal = "budget_stopped"
    else:
        terminal = "failed"
        if not errors:
            errors = errors + ["no final report was written"]

    record.update(classification.as_record_fields())

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
            "pricing": pricing_record(config, model, cost, basis).model_dump(mode="json"),
            "tool_calls": (
                summary.get("tool_calls")
                if summary.get("tool_calls") is not None
                else (totals.get("tool_calls") if totals else None)
            ),
            "recovery_rounds": summary.get("recovery_rounds"),
            "revision_rounds": summary.get("revision_rounds"),
            "evidence_count": len(capture["evidence"]),
            # Written from the citations actually captured, so a record can
            # never claim no citations while carrying a non-empty list.
            "citation_count": len(capture["citations"]),
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
    #: Resolved once per batch: which model each half of the pipeline calls.
    #: Passed in rather than recomputed per run so one batch cannot drift.
    provenance: ModelProvenance = field(default_factory=ModelProvenance)
    #: The account each run key was assigned, decided before the batch starts.
    #: Empty for a single-credential batch. A run looks itself up here and never
    #: re-decides its account in response to a quota error.
    allocations: Mapping[str, str] = field(default_factory=dict)
    #: Authorised accounts by label. Never written to a record: only
    #: ``Account.public()`` is.
    accounts: Mapping[str, Any] = field(default_factory=dict)

    def account_for(self, run_key: str) -> Any:
        """The account for this attempt, or None for an undeclared batch."""
        label = self.allocations.get(run_key)
        return self.accounts.get(label) if label else None


class InProcessExecutor:
    """Calls ``athena.run.run_one`` in this process (the preferred path)."""

    backend = "in_process"

    def __init__(self, spec: ExecutorSpec) -> None:
        self.spec = spec

    def __call__(
        self, entry: PlanEntry, item: EvalItem, *, attempt: int = 1, attempt_id: str = ""
    ) -> dict[str, Any]:
        from athena.run import run_one  # imported here so --dry-run needs no LLM stack

        record = base_record(
            entry, item, self.spec.config,
            model=self.spec.model, timeout_s=self.spec.timeout_s, backend=self.backend,
            attempt=attempt, attempt_id=attempt_id, provenance=self.spec.provenance,
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

    def __call__(
        self, entry: PlanEntry, item: EvalItem, *, attempt: int = 1, attempt_id: str = ""
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        record = base_record(
            entry, item, self.spec.config,
            model=self.spec.model, timeout_s=self.spec.timeout_s, backend=self.backend,
            attempt=attempt, attempt_id=attempt_id, provenance=self.spec.provenance,
        )
        errors: list[str] = []
        notes: list[str] = []
        log_path = self.spec.log_dir / f"{entry.run_key}.log"
        # A retry must not overwrite the previous attempt's summary, or the second
        # attempt would silently inherit the first one's numbers.
        suffix = f".a{attempt}" if attempt > 1 else ""
        summary_path = self.spec.log_dir / f"{entry.run_key}{suffix}.summary.json"
        argv = [
            sys.executable, "-m", "athena.run", item.prompt,
            "--pattern", entry.pattern, "--run-id", run_id,
            "--json-summary", str(summary_path),
        ]
        started = time.perf_counter()
        timed_out = False
        returncode: int | None = None
        # Attribute the attempt to its pre-assigned account and give the child
        # that account's credential. The account was chosen before the batch
        # started (see evaluation.accounts); it is never re-chosen in response to
        # a quota error, which would be rotating to evade enforcement.
        account = self.spec.account_for(entry.run_key)
        child_env = dict(os.environ)
        if account is not None:
            child_env["LLM_API_KEY"] = account.key
            child_env["GROQ_API_KEY"] = account.key
            child_env["LLM_MODEL"] = account.model
            if account.api_base:
                child_env["LLM_API_BASE"] = account.api_base
            record.update(account.public())
        try:
            with log_path.open("a" if attempt > 1 else "w", encoding="utf-8",
                               errors="replace") as log:
                process = subprocess.Popen(
                    argv, cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT,
                    env=child_env,
                )
                try:
                    process.wait(timeout=self.spec.timeout_s)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    process.kill()
                    process.wait(timeout=30)
                returncode = process.returncode
        except OSError as exc:
            # Spawning itself failed (missing interpreter, descriptor exhaustion):
            # infrastructure, recorded rather than raised so the batch survives.
            returncode = None
            errors.append(f"could not start the child process: {type(exc).__name__}: {exc}")
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
            if not errors:
                errors.append(
                    f"child exited {returncode} without writing a summary"
                )

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
    """Run keys that must be skipped on resume.

    A key counts as done when its best recorded attempt is a final result
    (``completed``/``failed``). With ``retry_failed`` a key whose only attempts
    ended in a transient infrastructure state stays eligible, because that is
    exactly the case a retry exists for.
    """
    best: dict[str, list[str]] = {}
    for record in records:
        key = str(record.get("run_key") or "")
        if not key:
            continue
        best.setdefault(key, []).append(str(record.get("terminal_state") or ""))
    keys: set[str] = set()
    for key, states in best.items():
        if retry_failed and not any(state in schema.FINAL_RUN_STATES for state in states):
            continue
        keys.add(key)
    return keys


def attempts_by_key(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How many attempts each run key already has on disk (for resume numbering)."""
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("run_key") or "")
        if not key:
            continue
        try:
            number = int(record.get("attempt") or 1)
        except (TypeError, ValueError):
            number = 1
        counts[key] = max(counts.get(key, 0), number)
    return counts


def should_retry(record: Mapping[str, Any]) -> bool:
    """Only a transient *infrastructure* failure is worth re-executing.

    A critic rejection, thin evidence, a completed answer, a budget stop or a
    pipeline-level failure are results. Re-running them to get a nicer sample
    would corrupt the comparison, so they are never retried.
    """
    state = str(record.get("terminal_state") or "")
    if state not in schema.TRANSIENT_INFRA_STATES:
        return False
    if record.get("critic_status") or record.get("critic_approved") is not None:
        return False
    return True


class BudgetTracker:
    """Stops the batch instead of letting it spend without limit.

    ``max_runs`` counts **attempts**, not plan entries, so a retry consumes
    budget: a batch cannot exceed its hard attempt cap by retrying.

    The USD caps are applied to whatever cost the records actually carry. When the
    model is unpriced every cost is ``None``, so the running total stays zero and
    a dollar cap can never fire; ``usd_enforceable`` reports that truth in
    ``run_meta.json`` rather than letting a zero total imply safety. Enforcement
    itself is never switched off, because a run that *does* report a cost must
    still count against the cap.
    """

    def __init__(self, budget: schema.Budget, *, usd_enforceable: bool = True) -> None:
        self.budget = budget
        self.usd_enforceable = usd_enforceable
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
            "usd_cap_enforceable": self.usd_enforceable,
            "usd_cap_note": (
                "the configured model(s) are priced, so the USD caps act on estimates"
                if self.usd_enforceable
                else "the configured model(s) are unpriced: every cost is null, so the "
                "USD caps collect nothing and cannot bound real spend - max_runs is the "
                "only effective cap. A recorded cost, if any, still counts."
            ),
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
        executor: Callable[..., dict[str, Any]] | None = None,
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
        provenance: ModelProvenance | None = None,
        acknowledge_unpriced: bool = False,
        max_attempts_per_run: int = DEFAULT_MAX_ATTEMPTS_PER_RUN,
        accounts: Sequence[Any] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.dataset = dataset
        self.config = config
        self.out_dir = out_dir
        self.backend = (backend or config.matrix.backend)
        self.timeout_s = float(timeout_s or config.matrix.per_run_timeout_s)
        self.resume = resume
        self.retry_failed = retry_failed
        self.limit = limit
        # B2: every progress line is flushed, so a redirected log shows work as it
        # happens. A caller may still inject its own collector.
        self.logger = logger if logger is not None else flushed_logger
        self.entries = list(entries) if entries is not None else list(config.entries())
        self.config_path = config_path or DEFAULT_CONFIG
        self.dataset_path = dataset_path or DEFAULT_DATASET
        self.acknowledge_unpriced = acknowledge_unpriced
        self.max_attempts_per_run = max(int(max_attempts_per_run), 1)

        # One resolution for the whole batch: the two halves of the pipeline can
        # be on different models, and every record must say which is which.
        self.provenance = provenance if provenance is not None else resolve_provenance()
        self.model = model or self.provenance.adk_model or os.getenv("LLM_MODEL") or "default"

        budget = config.budget.model_copy(
            update={
                "max_runs": max_runs if max_runs is not None else config.budget.max_runs,
                "max_total_usd": max_usd if max_usd is not None else config.budget.max_total_usd,
            }
        )
        self.usd_enforceable = usd_cap_is_enforceable(
            config, self.model, self.provenance.crew_model
        )
        self.budget = BudgetTracker(budget, usd_enforceable=self.usd_enforceable)

        if executor is None:
            accounts = tuple(accounts or ())
            allocations = (
                accounts_module.allocation_for(
                    (entry.run_key for entry in self.entries), accounts
                )
                if accounts else {}
            )
            self.accounts = accounts
            self.allocations = allocations
            spec = ExecutorSpec(
                config=config, dataset=dataset, timeout_s=self.timeout_s,
                log_dir=out_dir / "logs", model=self.model,
                provenance=self.provenance,
                allocations=allocations,
                accounts={account.label: account for account in accounts},
            )
            self.executor = build_executor(spec, self.backend)
        else:
            self.accounts = tuple(accounts or ())
            self.allocations = {}
            self.executor = executor

        self.raw_path = out_dir / "raw.jsonl"
        self.records: list[dict[str, Any]] = []
        self.accounting = AttemptAccounting(
            planned=len(self.entries), hard_max_attempts=budget.max_runs
        )
        self._seen_attempt_ids: set[str] = set()

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
        # Attempt numbering continues from what is already on disk, so a resumed
        # retry never reuses attempt 1's id or overwrites its record.
        self._prior_attempts = attempts_by_key(existing) if self.resume else {}
        self._seen_attempt_ids = {
            str(record.get("attempt_id"))
            for record in existing
            if record.get("attempt_id")
        }
        return [entry for entry in entries if entry.run_key not in skipped], skipped

    # --- execution ------------------------------------------------------
    def run(self) -> dict[str, Any]:
        # A bad --out is a precondition, not a crash (B4): one readable message,
        # no partial run_meta.json, EXIT_INVALID from main().
        prepare_out_dir(self.out_dir)

        entries = self.selected()
        pending, skipped = self.pending(entries)
        self.accounting.planned = len(entries)
        self.accounting.skipped = len(skipped)
        self.logger(
            f"plan {self.config.plan.id}: {len(entries)} entries, "
            f"{len(skipped)} already recorded, {len(pending)} to run"
        )
        self.logger(
            f"models: adk={self.provenance.adk_model or '(unset)'} "
            f"crew={self.provenance.crew_model or '(unset)'} "
            f"profile={'applied' if self.provenance.profile_applied else 'none'}"
            + ("  [ADK and crew differ]" if self.provenance.models_differ else "")
        )
        for warning in self.preflight_warnings():
            self.logger(f"warning: {warning}")

        meta = self._meta(entries, skipped)
        self._write_meta(meta)

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
            try:
                self._run_entry(entry, item, index=index, total=len(pending))
            except KeyboardInterrupt:
                stopped_reason = "interrupted"
                remaining = pending[index - 1:]
                raise
            except _BudgetStop as stop:
                stopped_reason = stop.reason
                remaining = pending[index - 1:]
                self.logger(f"stopping: {stop.reason}")
                break

        meta.update(
            {
                "ended_at": utc_now(),
                "records_written": len(self.records),
                "budget": self.budget.as_dict(),
                "attempts": self.accounting.model_dump(mode="json"),
                "stopped_reason": stopped_reason,
            }
        )
        self._write_meta(meta)
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
            "attempts": self.accounting.model_dump(mode="json"),
            "states": _count_states(self.records),
        }
        self.logger(json.dumps(summary, indent=2))
        return summary

    # --- one entry, with bounded retries --------------------------------
    def _run_entry(
        self, entry: PlanEntry, item: EvalItem, *, index: int, total: int
    ) -> None:
        """Execute one plan entry, retrying only transient infrastructure faults."""
        prior = getattr(self, "_prior_attempts", {}).get(entry.run_key, 0)
        attempt = prior + 1
        while True:
            reason = self.budget.exhausted()
            if reason:
                raise _BudgetStop(reason)

            attempt_id = new_attempt_id(entry.run_key, attempt)
            while attempt_id in self._seen_attempt_ids:  # pragma: no cover - uuid4 collision
                attempt_id = new_attempt_id(entry.run_key, attempt)
            self._seen_attempt_ids.add(attempt_id)

            # B2: a progress line before the attempt, flushed, so a redirected
            # log shows work while it is happening rather than only at exit.
            self.logger(
                f"[{index}/{total}] {entry.run_key} attempt {attempt} "
                f"({item.category}, {item.difficulty}) starting - "
                f"id={attempt_id}"
            )
            try:
                record = self._call_executor(entry, item, attempt=attempt,
                                             attempt_id=attempt_id)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - never lose the batch to one run
                record = self._harness_failure(entry, item, exc, attempt=attempt,
                                               attempt_id=attempt_id)
            record = self._stamp(record, attempt=attempt, attempt_id=attempt_id)

            self._append(record)
            self.records.append(record)
            self._account(record)
            over_budget = self.budget.record(record)
            self.logger(
                f"    -> attempt {attempt} {record.get('terminal_state')} "
                f"{record.get('latency_s')}s "
                f"{(record.get('est_cost_usd') if record.get('est_cost_usd') is not None else 'cost unpriced')}"
            )
            if over_budget:
                raise _BudgetStop(over_budget)

            if not (self.retry_failed and should_retry(record)):
                return
            if attempt >= self.max_attempts_per_run:
                self.logger(
                    f"    -> not retrying {entry.run_key}: reached "
                    f"max_attempts_per_run={self.max_attempts_per_run}"
                )
                return
            attempt += 1
            self.accounting.retries += 1
            self.logger(
                f"    -> transient infrastructure failure; retrying as attempt {attempt}"
            )

    def _call_executor(
        self, entry: PlanEntry, item: EvalItem, *, attempt: int, attempt_id: str
    ) -> dict[str, Any]:
        """Call the executor, supporting both the new and the simple signature.

        Tests inject ``Callable[[PlanEntry, EvalItem], dict]``; the built-in
        executors also accept ``attempt``/``attempt_id``. Supporting both keeps a
        stub executor a one-liner without losing attempt identity in real runs.
        """
        try:
            return self.executor(entry, item, attempt=attempt, attempt_id=attempt_id)
        except TypeError as exc:
            if "attempt" not in str(exc):
                raise
            return self.executor(entry, item)

    def _stamp(self, record: dict[str, Any], *, attempt: int, attempt_id: str) -> dict[str, Any]:
        """Guarantee the attempt identity and provenance on a returned record.

        An injected executor may hand back a record that predates these fields (or
        carries only the schema's empty placeholder). The batch is the authority on
        *which* attempt this was and *which* models were resolved, so it fills them
        in rather than recording blanks.

        B1: ``config`` names the same five provenance facts ``provenance`` carries -
        adk_model, crew_model, provider, the redacted api_host and profile_applied -
        so a reader that only looks at ``config`` still cannot mistake one model
        label for the whole pipeline. No key value is ever placed here.
        """
        record.setdefault("attempt", attempt)
        if not record.get("attempt_id"):
            record["attempt_id"] = attempt_id
        provenance = record.get("provenance")
        if not provenance or not (
            isinstance(provenance, Mapping) and provenance.get("adk_model")
        ):
            record["provenance"] = self.provenance.model_dump(mode="json")
        config = record.get("config")
        if isinstance(config, dict):
            for field_name in (
                "adk_model", "crew_model", "provider", "api_host", "profile_applied",
                "env_source",
            ):
                if not config.get(field_name):
                    config[field_name] = getattr(self.provenance, field_name)
            config.setdefault("seed_propagated_to_provider",
                              schema.SEED_PROPAGATED_TO_PROVIDER)
        return record

    def _account(self, record: Mapping[str, Any]) -> None:
        self.accounting.attempted += 1
        state = str(record.get("terminal_state") or "")
        if state == "completed":
            self.accounting.completed += 1
        elif state == "failed":
            self.accounting.failed += 1
        elif state == "timeout":
            self.accounting.timeout += 1
        else:
            self.accounting.infra_errors += 1

    def preflight_warnings(self) -> list[str]:
        """Things that make this batch weaker than it looks, said out loud."""
        warnings: list[str] = []
        missing = unpriced_models(
            self.config, self.model, self.provenance.crew_model
        )
        if missing:
            warnings.append(
                f"unpriced model(s) {missing}: every cost field will be null and the "
                f"USD caps (max_total_usd={self.config.budget.max_total_usd}, "
                f"max_run_usd={self.config.budget.max_run_usd}) cannot bound real spend. "
                f"max_runs={self.config.budget.max_runs} is the only hard cap."
            )
            if not self.acknowledge_unpriced:
                warnings.append(
                    "start a live batch over unpriced models with "
                    "--acknowledge-unpriced so the acknowledgement is recorded"
                )
        if self.backend != "subprocess":
            warnings.append(
                f"backend={self.backend}: only the subprocess backend is proven to keep "
                "MCP stdio toolsets alive; the in-process backend redirects stdout and "
                "previously produced zero-evidence runs"
            )
        if self.provenance.models_differ:
            warnings.append(
                f"ADK and CrewAI models differ: adk={self.provenance.adk_model} "
                f"crew={self.provenance.crew_model} - both are recorded per run"
            )
        return warnings

    def _meta(self, entries: Sequence[PlanEntry], skipped: Iterable[str]) -> dict[str, Any]:
        return {
            "harness_version": __version__,
            "started_at": utc_now(),
            "git_commit": git_commit(),
            "python": sys.version.split()[0],
            "backend": self.backend,
            # `model` is kept for the analyser, but it is only the ADK half; the
            # provenance block below is the authoritative answer (B1).
            "model": self.model,
            "adk_model": self.provenance.adk_model,
            "crew_model": self.provenance.crew_model,
            "provider": self.provenance.provider,
            "crew_provider": self.provenance.crew_provider,
            "api_host": self.provenance.api_host,
            "crew_api_host": self.provenance.crew_api_host,
            "profile_applied": self.provenance.profile_applied,
            "profile_id": self.provenance.profile_id,
            "profile_name": self.provenance.profile_name,
            "env_source": self.provenance.env_source,
            "models_differ": self.provenance.models_differ,
            "provenance": self.provenance.model_dump(mode="json"),
            "seed_propagated_to_provider": schema.SEED_PROPAGATED_TO_PROVIDER,
            "seed_note": (
                "Seeds are trial labels only. The pipeline never passes a seed to a "
                "provider, so a variance tier is a stochastic replicate, not a seeded "
                "reproduction, and its spread is sampling noise."
                if not schema.SEED_PROPAGATED_TO_PROVIDER
                else "Seeds are forwarded to the provider."
            ),
            "timeout_s": self.timeout_s,
            "dataset_path": str(self.config.dataset_path),
            "dataset_sha256": _digest(self.dataset_path),
            "config_sha256": _digest(self.config_path),
            "dataset_items": len(self.dataset.items),
            "plan_id": self.config.plan.id,
            "plan_scope": self.config.plan.scope,
            "plan_target_runs": self.config.plan.target,
            "plan_entries": len(entries),
            "already_recorded": sorted(skipped),
            "pricing": pricing_status(),
            "pricing_status": pricing_status_for_meta(
                self.config, self.model, self.provenance.crew_model
            ),
            "unpriced_acknowledged": {
                "acknowledged": self.acknowledge_unpriced,
                "unpriced_models": unpriced_models(
                    self.config, self.model, self.provenance.crew_model
                ),
                "note": (
                    "The operator was told that costs are null and the USD caps are "
                    "inert for this batch, and chose to proceed."
                    if self.acknowledge_unpriced
                    else "No acknowledgement was given; a live batch over unpriced "
                    "models is refused without --acknowledge-unpriced."
                ),
            },
            "budget": self.budget.as_dict(),
            "attempts": self.accounting.model_dump(mode="json"),
            "riders": {
                "backend_note": (
                    "subprocess: the same entry point the demo HTTP API uses"
                    if self.backend == "subprocess"
                    else "in_process: redirects this process's stdout/stderr; MCP stdio "
                    "toolsets are known to fail there"
                ),
            },
            "stopped_reason": None,
            "records_written": 0,
        }

    def _write_meta(self, meta: Mapping[str, Any]) -> None:
        (self.out_dir / "run_meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )

    def _harness_failure(
        self,
        entry: PlanEntry,
        item: EvalItem,
        exc: Exception,
        *,
        attempt: int = 1,
        attempt_id: str = "",
    ) -> dict[str, Any]:
        """A failure raised outside the run itself is recorded, never hidden."""
        record = base_record(
            entry, item, self.config,
            model=self.model, timeout_s=self.timeout_s, backend=self.backend,
            attempt=attempt, attempt_id=attempt_id, provenance=self.provenance,
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
        """Append one record and make it durable before the next attempt starts."""
        with self.raw_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class _BudgetStop(RuntimeError):
    """Internal: the batch hit a cap while working through one entry."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    env_file = load_env()
    profile_env, profile_source = active_profile_env()
    crew_model = (profile_env.get("LLM_MODEL") or os.getenv("LLM_MODEL") or "default")
    status: dict[str, Any] = {
        "env_file": env_file,
        "crew_a2a_server": "unknown",
        "llm_api_key": "present" if os.getenv("LLM_API_KEY") else "absent",
        "crew_llm_api_key": (
            "present" if (profile_env.get("LLM_API_KEY") or os.getenv("LLM_API_KEY"))
            else "absent"
        ),
        "model": os.getenv("LLM_MODEL") or "default",
        "crew_model": crew_model,
        "profile_env": profile_source,
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
    parser.add_argument(
        "--accounts", default="",
        help=(
            "comma-separated settings-profile names to spread this batch across. "
            "Each profile must hold a key; all must share one endpoint and one "
            "model, or the batch is refused. The allocation is computed from the "
            "plan before the first run and recorded per attempt. Omit for a "
            "single-credential batch."
        ),
    )
    parser.add_argument(
        "--acknowledge-unpriced", action="store_true",
        help=(
            "required before a live batch whose model(s) no price table can price. "
            "The cost fields will all be null and the USD caps cannot bound real "
            "spend; this records that the operator was told and chose to proceed."
        ),
    )
    parser.add_argument(
        "--max-attempts-per-run", type=int, default=DEFAULT_MAX_ATTEMPTS_PER_RUN,
        help=(
            "executions allowed for one run key, retries included "
            f"(default {DEFAULT_MAX_ATTEMPTS_PER_RUN}); only transient infrastructure "
            "failures are ever retried"
        ),
    )
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
    provenance: ModelProvenance | None = None,
    acknowledge_unpriced: bool = False,
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

    resolved = provenance if provenance is not None else ModelProvenance(adk_model=model)
    prompts = sorted({entry.prompt_id for entry in entries})
    missing = unpriced_models(config, model, resolved.crew_model)
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
            "scope": config.plan.scope,
            "minimum_runs": config.plan.minimum_runs,
            "target_runs": config.plan.target,
            "committed_entries": len(config.entries()),
            "selected_entries": len(entries),
            "distinct_prompts": len(prompts),
            "by_pattern": by_pattern,
            "by_tier": by_tier,
            "by_category": by_category,
            "first_entries": [entry.run_key for entry in entries[:5]],
            "note": config.plan.note,
        },
        "run": {
            "backend": backend,
            "model": model,
            "adk_model": resolved.adk_model,
            "crew_model": resolved.crew_model,
            "models_differ": resolved.models_differ,
            "provider": resolved.provider,
            "api_host": resolved.api_host,
            "crew_api_host": resolved.crew_api_host,
            "profile_applied": resolved.profile_applied,
            "env_source": resolved.env_source,
            "per_run_timeout_s": timeout_s,
            "seed_propagated_to_provider": schema.SEED_PROPAGATED_TO_PROVIDER,
            "pricing_note": config.pricing.note,
            "pricing": pricing_status(),
            "pricing_summary": pricing_summary(config, model),
        },
        "honesty": {
            "unpriced_models": missing,
            "usd_cap_enforceable": not missing,
            "unpriced_acknowledged": acknowledge_unpriced,
            "usd_cap_note": (
                "The USD caps cannot bound real spend for this batch: every cost is "
                "null, so a running total is 0 by construction. max_runs is the only "
                "hard cap."
                if missing
                else "Every batch model is priced, so the USD caps act on estimates."
            ),
            "seed_note": (
                "No seed is forwarded to the provider; a variance tier measures "
                "stochastic spread, not reproducibility."
                if not schema.SEED_PROPAGATED_TO_PROVIDER
                else "Seeds are forwarded to the provider."
            ),
        },
        "budget": config.budget.model_dump(mode="json"),
        "preconditions": dict(status),
    }


def configure_streams() -> None:
    """Keep stdout/stderr unbuffered and write-through after reconfiguring (B2).

    ``stream.reconfigure(encoding=...)`` re-wraps the stream, and on Windows that
    drops the write-through mode ``python -u`` asked for: a redirected
    ``> batch.log`` then stays empty until the process exits, so a long batch looks
    dead while it is working. ``write_through=True`` keeps every line visible
    immediately, and the explicit flush afterwards covers a stream that does not
    accept the keyword.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:  # pragma: no cover - a detached GUI process
            continue
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(
                    encoding="utf-8", errors="replace", write_through=True
                )
            except (ValueError, TypeError):  # pragma: no cover - exotic stream
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001 - never fail over a log stream
                    pass
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass


def flushed_logger(message: str, *, stream=None) -> None:
    """Print one progress line and flush it, so a redirect shows it immediately."""
    target = stream if stream is not None else sys.stdout
    print(message, file=target)
    try:
        target.flush()
    except Exception:  # noqa: BLE001 - a broken stdout must not end a batch
        pass


def main(argv: list[str] | None = None) -> int:
    configure_streams()

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
    provenance = resolve_provenance()
    model = args.model or provenance.adk_model or (os.getenv("LLM_MODEL") or "default")
    if not provenance.adk_model:
        provenance = provenance.model_copy(update={"adk_model": model})

    if args.dry_run:
        status = preflight(timeout_s=1.0)
        payload = describe_plan(
            dataset, config, entries, status,
            timeout_s=timeout_s, backend=backend, model=model,
            provenance=provenance, acknowledge_unpriced=args.acknowledge_unpriced,
        )
        if args.json_out:
            print(json.dumps(payload, indent=2))
            return EXIT_OK
        print("DRY RUN - nothing was executed and no files were written\n")
        print(f"dataset   : {payload['dataset']['items']} items, "
              f"{len(payload['dataset']['categories'])} categories")
        print(f"plan      : {payload['plan']['id']} - {payload['plan']['selected_entries']} "
              f"runs from {payload['plan']['distinct_prompts']} prompts "
              f"(scope={payload['plan']['scope']}, "
              f"target={payload['plan']['target_runs']}, "
              f"min={payload['plan']['minimum_runs']})")
        print("patterns  : " + ", ".join(
            f"{name}={count}" for name, count in sorted(payload["plan"]["by_pattern"].items())
        ))
        print("tiers     : " + ", ".join(
            f"{name}={count}" for name, count in sorted(payload["plan"]["by_tier"].items())
        ))
        by_category = payload["plan"]["by_category"]
        print(
            f"categories: {len(by_category)} present, "
            f"{min(by_category.values())}-{max(by_category.values())} runs each ("
            + ", ".join(f"{name}={count}" for name, count in sorted(by_category.items()))
            + ")"
        )
        keyed = [entry.run_key for entry in entries]
        print(f"run keys  : {len(keyed)} scheduled, {len(set(keyed))} distinct")
        print("run       : backend=" + backend + f" model={model} timeout={timeout_s:.0f}s")
        print(
            "models    : "
            f"adk={provenance.adk_model or '(unset)'} "
            f"crew={provenance.crew_model or '(unset)'} "
            f"profile={'applied' if provenance.profile_applied else 'none'}"
            + ("  [they differ]" if provenance.models_differ else "")
        )
        print(f"budget    : <= {config.budget.max_runs} attempts, "
              f"<= {config.budget.max_total_usd} USD, <= {config.budget.max_run_usd} USD/run")
        price = payload["run"].get("pricing_summary") or {}
        print(f"pricing   : {price.get('basis', 'unknown')}")
        print(f"honesty   : {payload['honesty']['usd_cap_note']}")
        print(f"seeds     : {payload['honesty']['seed_note']}")
        print("first 5   : " + ", ".join(payload['plan']['first_entries']))
        print("\npreconditions (a live batch needs both):")
        for name, value in status.items():
            print(f"  {name:<18} {value}")
        if status.get("crew_a2a_server") != "up" or status.get("llm_api_key") != "present":
            print("\nnote: preconditions are not satisfied. That is fine for a dry run; "
                  "a live batch will refuse to start.")
        if payload["honesty"]["unpriced_models"] and not args.acknowledge_unpriced:
            print("\nnote: this batch's model(s) are unpriced; a live batch also needs "
                  "--acknowledge-unpriced.")
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

    # --- the results directory must exist and be writable (B4) -----------
    # Checked here, before any gate that can still refuse the batch, because a
    # bad --out used to escape later as a raw PermissionError traceback with no
    # record and no message. A failed creation writes nothing at all - not even a
    # partial run_meta.json - and maps to EXIT_INVALID.
    try:
        prepare_out_dir(out_dir)
    except OutputDirectoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID

    # --- unpriced acknowledgement gate (8) -------------------------------
    # An unpriced batch records cost null everywhere, so summing costs can never
    # trip the dollar caps. Refusing by default is the only honest option.
    missing = unpriced_models(config, model, provenance.crew_model)
    if missing and not args.acknowledge_unpriced:
        print(
            f"error: no price table can price {missing}, so every run would record "
            "est_cost_usd=null and the USD caps "
            f"(max_total_usd={config.budget.max_total_usd}, "
            f"max_run_usd={config.budget.max_run_usd}) cannot bound real spend - a "
            "running total of nulls is zero. "
            f"max_runs={config.budget.max_runs} is the only hard cap.\n"
            "Do not invent a rate. If you accept that, rerun with "
            "--acknowledge-unpriced; the acknowledgement is recorded in run_meta.json.",
            file=sys.stderr,
        )
        discard_empty_out_dir(out_dir)
        return EXIT_PRECONDITION
    if missing:
        print(
            f"note: unpriced model(s) {missing} acknowledged; costs will be null and the "
            "USD caps are inert for this batch.",
            file=sys.stderr,
        )

    price = pricing_summary(config, model)
    if not price["priced"]:
        print(
            f"warning: model {model!r} has no rate in evaluation/config.json pricing or "
            "data/pricing.json. Every run will record cost null and the cost charts will be "
            "empty. Add a rate for it (or run a model that is listed) if cost matters.",
            file=sys.stderr,
        )

    try:
        authorised = load_accounts(_split(args.accounts))
    except accounts_module.AccountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    if authorised:
        print(
            "accounts  : "
            + ", ".join(f"{a.label} ({accounts_module.account_fingerprint(a.key)})"
                        for a in authorised)
            + f" - {len(authorised)} credential(s), one endpoint, one model",
            flush=True,
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
        provenance=provenance,
        acknowledge_unpriced=args.acknowledge_unpriced,
        max_attempts_per_run=args.max_attempts_per_run,
        accounts=authorised,
    )

    try:
        runner.run()
    except OutputDirectoryError as exc:
        # B4: a clear message and EXIT_INVALID, no partial run_meta.json.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except KeyboardInterrupt:
        print("\ninterrupted: records written so far are kept; rerun to resume.",
              file=sys.stderr)
        return EXIT_INTERRUPTED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
