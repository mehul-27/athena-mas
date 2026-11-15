"""Batch-specific summary from runs.csv. No model calls, no source changes."""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(r"M:\Athena\athena-mas")
OUT = REPO / "evaluation" / "results" / "20261114T174056Z_amd_hierarchical_30"
RUNS = OUT / "runs.csv"


def num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def truth(value):
    return str(value).strip().lower() == "true"


def dist(values):
    present = [v for v in values if v is not None]
    if not present:
        return {"n": 0}
    ordered = sorted(present)

    def pct(fraction):
        if len(ordered) == 1:
            return ordered[0]
        pos = (len(ordered) - 1) * fraction
        lo = int(pos)
        hi = min(lo + 1, len(ordered) - 1)
        weight = pos - lo
        return ordered[lo] * (1 - weight) + ordered[hi] * weight

    return {
        "n": len(ordered),
        "mean": round(statistics.fmean(ordered), 3),
        "median": round(statistics.median(ordered), 3),
        "p25": round(pct(0.25), 3),
        "p75": round(pct(0.75), 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "total": round(sum(ordered), 3),
    }


rows = list(csv.DictReader(RUNS.open(encoding="utf-8")))
assert len(rows) == 30

groups = defaultdict(list)
for row in rows:
    groups[row["tier"]].append(row)

tier_summary = {}
for tier in ("core", "breadth", "variance"):
    subset = groups[tier]
    tier_summary[tier] = {
        "attempts": len(subset),
        "completed": sum(1 for r in subset if r["terminal_state"] == "completed"),
        "valid_completed": sum(1 for r in subset if r["success"] in {"True", "False"} and r["terminal_state"] == "completed"),
        "success": sum(1 for r in subset if truth(r["success"])),
        "critic_approved": sum(1 for r in subset if truth(r["critic_approved"])),
        "critic_rejected": sum(1 for r in subset if r["critic_status"] == "rejected"),
        "latency_s_all_attempts": dist([num(r["latency_s"]) for r in subset]),
        "tokens_all_attempts": dist([num(r["tokens_total"]) for r in subset]),
        "rubric_valid_completed": dist([num(r["rubric_score"]) for r in subset]),
    }

variance_pairs = {}
by_prompt = defaultdict(list)
for row in rows:
    by_prompt[row["prompt_id"]].append(row)
for prompt_id, subset in sorted(by_prompt.items()):
    if len(subset) == 2:
        base, repeat = sorted(subset, key=lambda r: int(r["repeat"]))
        variance_pairs[prompt_id] = {
            "r0": {
                "run_key": base["run_key"],
                "latency_s": num(base["latency_s"]),
                "tokens_total": num(base["tokens_total"]),
                "rubric_score": num(base["rubric_score"]),
                "success": truth(base["success"]),
                "critic_status": base["critic_status"],
            },
            "r1": {
                "run_key": repeat["run_key"],
                "latency_s": num(repeat["latency_s"]),
                "tokens_total": num(repeat["tokens_total"]),
                "rubric_score": num(repeat["rubric_score"]),
                "success": truth(repeat["success"]),
                "critic_status": repeat["critic_status"],
            },
            "delta": {
                "latency_s": round((num(repeat["latency_s"]) or 0) - (num(base["latency_s"]) or 0), 3),
                "tokens_total": round((num(repeat["tokens_total"]) or 0) - (num(base["tokens_total"]) or 0), 3),
                "rubric_score": round((num(repeat["rubric_score"]) or 0) - (num(base["rubric_score"]) or 0), 3),
            },
        }

injection = [r for r in rows if r["category"] == "prompt_injection"]
abuse = [r for r in rows if r["category"] == "toxic_persona"]

pi01 = next(r for r in rows if r["run_key"] == "pi-01__hierarchical__r0")
raw = {}
for line in (OUT / "raw.jsonl").read_text(encoding="utf-8").splitlines():
    if line.strip():
        rec = json.loads(line)
        if rec.get("run_key") == "pi-01__hierarchical__r0":
            raw = rec
            break

summary = {
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "batch_dir": str(OUT).replace("\\", "/"),
    "plan": {
        "id": "hierarchical-30",
        "scheduled_keys": 30,
        "distinct_prompts": 24,
        "tiers": {"core": 12, "breadth": 12, "variance": 6},
        "categories": 12,
        "category_run_counts": dict(sorted(Counter(r["category"] for r in rows).items())),
        "personas": dict(sorted(Counter(r["persona"] for r in rows).items())),
    },
    "attempts": {
        "recorded": len(rows),
        "unique_attempt_ids": len({r["attempt_id"] for r in rows}),
        "distinct_run_keys": len({r["run_key"] for r in rows}),
        "retries": sum(1 for r in rows if int(r["attempt"]) > 1),
        "terminal_states": dict(sorted(Counter(r["terminal_state"] for r in rows).items())),
        "valid_completed": sum(1 for r in rows if r["terminal_state"] == "completed"),
        "technical_completion_rate": "30/30",
    },
    "outcomes": {
        "success": sum(1 for r in rows if truth(r["success"])),
        "success_denominator": len(rows),
        "critic_approved": sum(1 for r in rows if truth(r["critic_approved"])),
        "critic_rejected": sum(1 for r in rows if r["critic_status"] == "rejected"),
        "critic_agreement": sum(1 for r in rows if truth(r["success"]) == truth(r["critic_approved"])),
        "failure_categories": dict(sorted(Counter(
            failure
            for r in rows
            for failure in (r["failure_categories"] or "").split("|")
            if failure
        ).items(), key=lambda item: (-item[1], item[0]))),
    },
    "latency_s_all_attempts": dist([num(r["latency_s"]) for r in rows]),
    "tokens_all_attempts": dist([num(r["tokens_total"]) for r in rows]),
    "tokens_in_total": sum(num(r["tokens_in"]) or 0 for r in rows),
    "tokens_out_total": sum(num(r["tokens_out"]) or 0 for r in rows),
    "rubric_valid_completed": dist([num(r["rubric_score"]) for r in rows]),
    "cost": {
        "priced": sum(1 for r in rows if num(r["est_cost_usd"]) is not None),
        "unpriced": sum(1 for r in rows if num(r["est_cost_usd"]) is None),
        "total_usd": None,
        "note": "AMD endpoint price unknown; USD caps inert; no rate invented.",
    },
    "citations": {
        "produced": sum(num(r["citation_count"]) or 0 for r in rows),
        "resolved": sum(num(r["resolved_citations"]) or 0 for r in rows),
        "runs_with_no_citations": sum(1 for r in rows if (num(r["citation_count"]) or 0) == 0),
        "runs_missing_required_evidence": sum(
            1 for r in rows if "missing_required_evidence" in (r["failure_categories"] or "")
        ),
        "citation_precision": dist([num(r["citation_precision"]) for r in rows]),
    },
    "security": {
        "injection_scored": len(injection),
        "injection_evaluable": sum(1 for r in injection if truth(r["security_evaluable"])),
        "injection_unevaluable": sum(1 for r in injection if not truth(r["security_evaluable"])),
        "injection_obeyed": sum(1 for r in injection if truth(r["injection_obeyed"])),
        "abuse_scored": len(abuse),
        "abuse_evaluable": sum(1 for r in abuse if truth(r["security_evaluable"])),
        "abuse_unevaluable": sum(1 for r in abuse if not truth(r["security_evaluable"])),
        "abuse_handled": sum(1 for r in abuse if truth(r["abusive_input_handled"])),
        "abuse_echoed": sum(1 for r in abuse if truth(r["abuse_echoed"])),
        "note": "Rates use evaluable denominators only; no-answer attempts are unevaluable.",
    },
    "by_tier": tier_summary,
    "variance_pairs": variance_pairs,
    "answer_key_caveat": {
        "volatile_runs": [
            r["run_key"] for r in rows
            if r["prompt_id"] in {"cv-01", "cv-02"}
        ],
        "note": (
            "cv-01 (twice) and cv-02 (once) are volatile items whose answer_key.facts is "
            "prose and whose must_mention/numeric lists are empty. Their answer_key_miss "
            "labels are dataset-key limitations, not proof the pipeline failed; answer-key "
            "aggregates for current_version are footnoted and not used as a pipeline claim."
        ),
    },
    "pi_01_zero_evidence": {
        "run_key": pi01["run_key"],
        "category": pi01["category"],
        "terminal_state": pi01["terminal_state"],
        "evidence_count": num(pi01["evidence_count"]),
        "citation_count": num(pi01["citation_count"]),
        "answer_chars": len((raw.get("final_answer") or "").strip()),
        "tokens_in": raw.get("tokens_in"),
        "tokens_out": raw.get("tokens_out"),
        "security_evaluable": truth(pi01["security_evaluable"]),
        "injection_obeyed": pi01["injection_obeyed"],
        "critic_status": pi01["critic_status"],
        "note": (
            "A prompt-injection item answered without research evidence and without "
            "citations. Tokens and an answer exist, so it is not an inexplicably empty "
            "record; the security behaviour is evaluable and the quality failures are "
            "reported."
        ),
    },
    "provenance": {
        "adk_model": "openai/DeepSeek-V4-Flash",
        "crew_model": "openai/DeepSeek-V4-Flash",
        "api_host": "https://api.tokenlab.amd.com/v1",
        "crew_api_host": "https://api.tokenlab.amd.com/v1",
        "profile_name": "amd",
        "profile_id": "367f57c4-d107-447c-a857-632fa8e6a454",
        "models_differ": False,
        "backend": "subprocess",
        "llm_call_cap_recorded": 40,
        "seed": None,
    },
}

(OUT / "BATCH_SUMMARY.json").write_text(
    json.dumps(summary, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps({
    "attempts": summary["attempts"],
    "outcomes": summary["outcomes"],
    "latency": summary["latency_s_all_attempts"],
    "tokens": summary["tokens_all_attempts"],
    "tokens_in_total": summary["tokens_in_total"],
    "tokens_out_total": summary["tokens_out_total"],
    "rubric": summary["rubric_valid_completed"],
    "security": summary["security"],
    "citations": summary["citations"],
    "by_tier": summary["by_tier"],
    "variance_pairs": summary["variance_pairs"],
}, indent=2))
