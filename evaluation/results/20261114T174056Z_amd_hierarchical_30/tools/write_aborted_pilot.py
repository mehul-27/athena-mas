import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(r"M:\Athena\athena-mas")
OUT = REPO / "evaluation" / "results" / "20261114T174056Z_amd_hierarchical_30"
log = OUT / "logs" / "aborted_pilot_gf-01__hierarchical__r0_cap12_timeout300.log"
meta = OUT / "aborted_pilot_run_meta.json"
v1 = OUT / "hierarchical_30_amd_runtime.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


payload = {
    "aborted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "run_key": "gf-01__hierarchical__r0",
    "run_id": "4bb0e668-f9c5-4448-9da4-ad9eb3abd8a9",
    "attempt_id": "gf-01__hierarchical__r0#a1-27e21fa45cea",
    "attempt": 1,
    "profile": {
        "name": "amd",
        "id": "367f57c4-d107-447c-a857-632fa8e6a454",
        "model": "openai/DeepSeek-V4-Flash",
        "api_host": "https://api.tokenlab.amd.com/v1",
    },
    "effective_policy_at_abort": {
        "llm_call_cap": 12,
        "source": "stale .env ATHENA_MAX_LLM_CALLS overrides hierarchical default 40",
        "ATHENA_LLM_TIMEOUT_S": 300,
        "A2A_timeout_s": 240,
        "per_run_timeout_s": 3600,
        "backend": "subprocess",
        "config": "hierarchical_30_amd_runtime.json",
        "config_sha256": sha(v1),
    },
    "observed": {
        "started_at": "2026-11-14T17:43:53+00:00",
        "supervisor_stage_start": "2026-11-14T17:44:15.564796+00:00",
        "planner_stage_start": "2026-11-14T17:44:15.573212+00:00",
        "stage_end_events": 0,
        "terminal_record_written": False,
        "symptom": (
            "planner model call did not return within the 300 s provider timeout; "
            "LiteLLM emitted repeated feedback banners at timeout intervals and "
            "retried under num_retries=3; no stage_end or summary was produced"
        ),
        "endpoint_health_control": (
            "GET /v1/models returned HTTP 200 in ~1.1 s and listed "
            "DeepSeek-V4-Flash, so DNS, TLS, and the stored key are valid; the "
            "delay is generation latency, not auth or reachability"
        ),
    },
    "artifacts": {
        "preserved_log": str(log).replace("\\", "/"),
        "preserved_log_sha256": sha(log),
        "preserved_run_meta": str(meta).replace("\\", "/"),
        "preserved_run_meta_sha256": sha(meta),
    },
    "disposition": (
        "Pre-dataset aborted pilot. It produced no raw.jsonl record and is not "
        "counted as a scheduled-key attempt in the 30-key completeness audit; "
        "it is retained as operational evidence and reported separately. The "
        "batch was restarted under v2 with ATHENA_MAX_LLM_CALLS=40, "
        "ATHENA_LLM_TIMEOUT_S=900 and ATHENA_A2A_TIMEOUT_S=1200. No synthetic "
        "record was inserted and no scoring rule was changed."
    ),
    "reason_for_abort": (
        "Independent audit findings 2 and 3: the historical Groq diagnostic cap "
        "of 12 must not truncate a full hierarchical run (documented policy is "
        "40), and the 300 s provider timeout was demonstrably too short for AMD."
    ),
}
(OUT / "ABORTED_PILOT.json").write_text(
    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
)
print("wrote", OUT / "ABORTED_PILOT.json")
print("log_sha256", payload["artifacts"]["preserved_log_sha256"])
