import hashlib
import json
from pathlib import Path

REPO = Path(r"M:\Athena\athena-mas")
OUT = REPO / "evaluation" / "results" / "20261114T174056Z_amd_hierarchical_30"
v1 = OUT / "hierarchical_30_amd_runtime.json"
cfg = json.loads(v1.read_text(encoding="utf-8"))

assert len(cfg["plan"]["entries"]) == 30
assert len({e["run_key"] for e in cfg["plan"]["entries"]}) == 30
assert len({e["prompt_id"] for e in cfg["plan"]["entries"]}) == 24

cfg["name"] = "athena-eval-hierarchical-30-amd-runtime-v2"
cfg["matrix"]["llm_call_cap"] = 40
cfg["note"] = (
    cfg["note"]
    + " V2 correction (pre-dataset, after independent audit): the historical "
    ".env ATHENA_MAX_LLM_CALLS=12 is a stale Groq diagnostic override and "
    "silently replaced the documented hierarchical policy of 40 calls "
    "(athena.config.caps PATTERN_CAPS). The runtime launcher now exports "
    "ATHENA_MAX_LLM_CALLS=40 to the runner, every athena.run child, and the "
    "CrewAI server, and this config records llm_call_cap=40. Provider call "
    "timeout is 900 s and the A2A role timeout 1200 s, under the unchanged "
    "3600 s per-run wall. Token caps, critic/security thresholds, retry "
    "classification and the 36-attempt cap are unchanged."
)
dst = OUT / "hierarchical_30_amd_runtime_v2.json"
dst.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

dev = {
    "source_config": "evaluation/hierarchical_30.json",
    "source_sha256": hashlib.sha256(
        (REPO / "evaluation" / "hierarchical_30.json").read_bytes()
    ).hexdigest(),
    "runtime_config": str(dst).replace("\\", "/"),
    "runtime_sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
    "selection_changed": False,
    "planned_entries": len(cfg["plan"]["entries"]),
    "distinct_prompts": len({e["prompt_id"] for e in cfg["plan"]["entries"]}),
    "deviations": [
        {
            "field": "matrix.llm_call_cap",
            "from": "null (effective 12 from stale .env ATHENA_MAX_LLM_CALLS)",
            "to": 40,
            "reason": (
                "documented hierarchical policy in athena.config.caps; the "
                ".env 12 is a Groq diagnostic leftover and would truncate a "
                "full hierarchical run"
            ),
            "authorization": "independent audit finding 2; routine runtime config",
        },
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
            "field": "provider/A2A timeouts (runtime env, not config fields)",
            "from": "ATHENA_LLM_TIMEOUT_S=300 inherited; A2A default 240",
            "to": "ATHENA_LLM_TIMEOUT_S=900; ATHENA_A2A_TIMEOUT_S=1200",
            "reason": (
                "consistent timeout layers under the 3600 s per-run cap; "
                "provider timeout alone was not sufficient"
            ),
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
        "budget.max_runs": 36,
        "budget.max_total_usd": 8.0,
        "budget.max_run_usd": 0.6,
        "env_caps_kept": (
            "LLM_MAX_TOKENS=512 and role caps 1500/2000/3000; "
            "ATHENA_MAX_TOOL_CALLS=3, researcher/worker/calc model+tool caps; "
            "critic/security rules and scoring untouched"
        ),
    },
}
(OUT / "runtime_config_deviations_v2.json").write_text(
    json.dumps(dev, indent=2) + "\n", encoding="utf-8"
)
print("V2", dst)
print("SHA256", dev["runtime_sha256"])
