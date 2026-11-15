import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(r"M:\Athena\athena-mas")
src = REPO / "evaluation" / "hierarchical_30.json"
raw = src.read_text(encoding="utf-8")
cfg = json.loads(raw)
entries = cfg["plan"]["entries"]
assert len(entries) == 30, len(entries)
keys = [e["run_key"] for e in entries]
assert len(set(keys)) == 30 and len({e["prompt_id"] for e in entries}) == 24

stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = REPO / "evaluation" / "results" / f"{stamp}_amd_hierarchical_30"
out.mkdir(parents=True, exist_ok=False)

cfg["name"] = "athena-eval-hierarchical-30-amd-runtime"
cfg["matrix"]["per_run_timeout_s"] = 3600.0
cfg["budget"]["max_total_wall_s"] = 172800.0
cfg["note"] = (
    cfg["note"]
    + " RUNTIME COPY for the amd profile (openai/DeepSeek-V4-Flash at "
    "api.tokenlab.amd.com): the 30 plan entries, prompt selection, tiers, "
    "categories, seed-null interpretation, backend and all call/token caps are "
    "unchanged. Only the per-run timeout (1200->3600 s) and total wall budget "
    "(86400->172800 s) were raised for AMD latency; budget.max_runs remains 36 "
    "(30 scheduled keys + at most 6 transient-retry attempts), and no USD cap "
    "is enforceable because the endpoint publishes no price."
)
cfg["pricing"]["note"] = (
    "Deliberately empty for this batch: the amd profile model "
    "openai/DeepSeek-V4-Flash has no published list price on "
    "api.tokenlab.amd.com, so every run records est_cost_usd=null. The USD "
    "caps are inert; max_runs=36 is the only hard attempt cap. No AMD rate "
    "was invented."
)
dst = out / "hierarchical_30_amd_runtime.json"
dst.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

dev = {
    "source_config": "evaluation/hierarchical_30.json",
    "source_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    "runtime_config": str(dst).replace("\\", "/"),
    "runtime_sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
    "selection_changed": False,
    "planned_entries": len(entries),
    "distinct_prompts": len({e["prompt_id"] for e in entries}),
    "deviations": [
        {
            "field": "matrix.per_run_timeout_s",
            "from": 1200.0,
            "to": 3600.0,
            "reason": "AMD latency; slow runs must not be cut off by the wall clock",
        },
        {
            "field": "budget.max_total_wall_s",
            "from": 86400.0,
            "to": 172800.0,
            "reason": "48h batch wall budget so AMD slowness alone cannot abort",
        },
        {
            "field": "note/pricing.note",
            "from": "groq-specific text",
            "to": "amd-specific text",
            "reason": "honest provenance; pricing stays empty and costs null",
        },
    ],
    "unchanged_policy": {
        "backend": "subprocess",
        "seed": None,
        "llm_call_cap": None,
        "budget.max_runs": 36,
        "budget.max_total_usd": 8.0,
        "budget.max_run_usd": 0.6,
        "env_caps": (
            "ATHENA_MAX_LLM_CALLS=12 etc. untouched; LLM_MAX_TOKENS and role "
            "caps untouched"
        ),
    },
}
(out / "runtime_config_deviations.json").write_text(
    json.dumps(dev, indent=2) + "\n", encoding="utf-8"
)
print("OUT_DIR", out)
print("RUNTIME_SHA256", dev["runtime_sha256"])
print("SOURCE_SHA256", dev["source_sha256"])
print("PLANNED", dev["planned_entries"], "PROMPTS", dev["distinct_prompts"])
