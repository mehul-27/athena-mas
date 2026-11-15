"""Deterministic integrity audit for the hierarchical-30 batch. No model calls."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(r"M:\Athena\athena-mas")
OUT = REPO / "evaluation" / "results" / "20261114T174056Z_amd_hierarchical_30"
sys.path.insert(0, str(REPO))

from evaluation import outcomes  # noqa: E402


def load_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def main() -> int:
    records = load_records(OUT / "raw.jsonl")
    plan = json.loads((REPO / "evaluation" / "hierarchical_30.json").read_text(encoding="utf-8"))
    scheduled = [entry["run_key"] for entry in plan["plan"]["entries"]]
    scheduled_set = set(scheduled)

    by_key: dict[str, list[dict]] = {}
    for record in records:
        by_key.setdefault(str(record.get("run_key") or ""), []).append(record)

    attempt_ids = [str(r.get("attempt_id") or "") for r in records]
    duplicate_attempt_ids = sorted(
        attempt_id for attempt_id, count in Counter(attempt_ids).items() if count > 1
    )
    missing_keys = sorted(scheduled_set - set(by_key))
    extra_keys = sorted(set(by_key) - scheduled_set)

    valid = [outcomes.is_valid_completed(r) for r in records]
    states = Counter(str(r.get("terminal_state") or "unknown") for r in records)
    critics = Counter(str(r.get("critic_status") or "missing") for r in records)
    approvals = sum(1 for r in records if r.get("critic_approved") is True)
    evaluable = sum(1 for r in records if outcomes.critic_evaluable(r))

    completed_without_evidence = [
        r["run_key"] for r in records
        if r.get("terminal_state") == "completed" and not (r.get("evidence_count") or 0)
    ]
    completed_without_tokens = [
        r["run_key"] for r in records
        if r.get("terminal_state") == "completed"
        and not ((r.get("tokens_in") or 0) + (r.get("tokens_out") or 0))
    ]
    completed_without_model_calls = [
        r["run_key"] for r in records
        if r.get("terminal_state") == "completed" and not (r.get("model_calls") or 0)
    ]

    provenance = sorted({
        (
            r.get("provenance", {}).get("adk_model"),
            r.get("provenance", {}).get("crew_model"),
            r.get("provenance", {}).get("api_host"),
            r.get("provenance", {}).get("crew_api_host"),
            r.get("provenance", {}).get("profile_name"),
            r.get("provenance", {}).get("models_differ"),
        )
        for r in records
    })
    caps = sorted({r.get("config", {}).get("llm_call_cap") for r in records})
    null_costs = sum(1 for r in records if r.get("est_cost_usd") is None)

    per_key = {}
    for key in scheduled:
        rows = by_key.get(key, [])
        per_key[key] = {
            "attempts": len(rows),
            "attempt_ids": [r.get("attempt_id") for r in rows],
            "states": [r.get("terminal_state") for r in rows],
            "critic": [r.get("critic_status") for r in rows],
            "valid_completed": [outcomes.is_valid_completed(r) for r in rows],
        }

    audit = {
        "records": len(records),
        "scheduled_keys": len(scheduled),
        "distinct_scheduled_keys_seen": len(set(by_key) & scheduled_set),
        "missing_scheduled_keys": missing_keys,
        "extra_run_keys": extra_keys,
        "duplicate_attempt_ids": duplicate_attempt_ids,
        "states": dict(sorted(states.items())),
        "critic_status": dict(sorted(critics.items())),
        "critic_approved": approvals,
        "critic_evaluable": evaluable,
        "valid_completed": sum(valid),
        "completed_without_evidence": completed_without_evidence,
        "completed_without_tokens": completed_without_tokens,
        "completed_without_model_calls": completed_without_model_calls,
        "provenance_variants": provenance,
        "llm_call_caps_recorded": caps,
        "null_cost_records": null_costs,
        "per_key": per_key,
    }
    (OUT / "RUN_AUDIT.json").write_text(
        json.dumps(audit, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: audit[k] for k in (
        "records", "scheduled_keys", "distinct_scheduled_keys_seen",
        "missing_scheduled_keys", "duplicate_attempt_ids", "states",
        "critic_status", "critic_approved", "critic_evaluable",
        "valid_completed", "completed_without_evidence",
        "completed_without_tokens", "completed_without_model_calls",
        "provenance_variants", "llm_call_caps_recorded", "null_cost_records",
    )}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
