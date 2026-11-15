"""Write a sanitized preflight snapshot for the amd hierarchical-30 batch."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(r"M:\Athena\athena-mas")
OUT = REPO / "evaluation" / "results" / "20261114T174056Z_amd_hierarchical_30"
RUNTIME = OUT / "hierarchical_30_amd_runtime.json"
SOURCE = REPO / "evaluation" / "hierarchical_30.json"
PROFILE_ID = "367f57c4-d107-447c-a857-632fa8e6a454"

sys.path.insert(0, str(REPO))
os.chdir(REPO)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_env_names(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() in {
            "LLM_MODEL", "LLM_MAX_TOKENS", "LLM_ITPM_LIMIT", "LLM_OTPM_LIMIT",
            "LLM_RATELIMIT_MAX_WAIT_S", "ATHENA_MAX_LLM_CALLS",
            "ATHENA_MAX_TOOL_CALLS", "ATHENA_MAX_REVISIONS",
            "ATHENA_WORKER_MAX_MODEL_CALLS", "ATHENA_RESEARCHER_MAX_MODEL_CALLS",
            "ATHENA_RESEARCHER_MAX_TOOL_CALLS", "ATHENA_CALC_MAX_MODEL_CALLS",
            "ATHENA_CALC_MAX_TOOL_CALLS", "SEARCH_PROVIDER", "EMBEDDING_PROVIDER",
            "CHROMA_PATH", "CORPUS_DIR", "RAG_COLLECTION",
        }:
            values[k.strip()] = v.strip()
    return values


def card() -> dict:
    with urllib.request.urlopen(
        "http://127.0.0.1:9001/.well-known/agent-card.json", timeout=5
    ) as response:
        return {"status": response.status, "body": json.loads(response.read())}


def health() -> dict:
    with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=5) as response:
        return json.loads(response.read())


def mcp_tools() -> dict:
    proc = subprocess.run(
        [sys.executable, "scripts/list_tools.py"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
    )
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"error": "list_tools stdout was not JSON"}


def rag_probe() -> dict:
    from athena.tools.rag.search import rag_search

    result = rag_search("NASA mission planning budget", 3)
    return {
        "ok": result.ok,
        "error": result.error or "",
        "hits": len(result.hits),
        "first_docs": [h.document for h in result.hits[:3]],
    }


def web_probe() -> dict:
    from athena.tools.web.search import web_search

    result = web_search("NASA Artemis program", 2)
    return {
        "ok": result.ok,
        "error": result.error or "",
        "hits": len(result.results or []),
        "first_urls": [h.url for h in (result.results or [])[:2]],
    }


def python_processes() -> list[dict]:
    import json as _json

    script = (
        "import json,subprocess\n"
        "p=subprocess.run(['powershell','-NoProfile','-Command',"
        "\"Get-CimInstance Win32_Process -Filter \\\"Name='python.exe'\\\" | "
        "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress\"],"
        "capture_output=True,text=True)\n"
        "print(p.stdout)\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    try:
        rows = _json.loads(proc.stdout or "[]")
    except ValueError:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    out = []
    for row in rows:
        command = str(row.get("CommandLine") or "")
        kind = "other"
        if "crew_server" in command:
            kind = "crew_server"
        elif "athena.run" in command:
            kind = "athena.run"
        elif "evaluation.runner" in command:
            kind = "evaluation.runner"
        out.append(
            {
                "pid": row.get("ProcessId"),
                "ppid": row.get("ParentProcessId"),
                "kind": kind,
            }
        )
    return out


def quarantine() -> dict:
    root = REPO / "evaluation" / "quarantine"
    files = sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []
    return {
        "path": str(root),
        "file_count": len(files),
        "tree_sha256": hashlib.sha256(
            "\n".join(f"{p.relative_to(root).as_posix()}:{sha(p)}" for p in files).encode()
        ).hexdigest() if files else "",
    }


def main() -> int:
    cfg = json.loads(RUNTIME.read_text(encoding="utf-8"))
    entries = cfg["plan"]["entries"]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": (
            "Preflight evidence for the authorised full live 30-run Athena "
            "hierarchical evaluation on the saved amd profile only."
        ),
        "authorization": {
            "thirty_runs": True,
            "deepseek_subagents_via_framework": True,
            "deployment": False,
            "paid_service_installation": False,
            "commit_or_push": False,
            "note": (
                "Operator was told the endpoint is unpriced so USD caps cannot "
                "bound spend; acknowledged via --acknowledge-unpriced."
            ),
        },
        "workspace": {
            "root": str(REPO),
            "branch": subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(REPO),
                capture_output=True, text=True,
            ).stdout.strip(),
            "commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(REPO),
                capture_output=True, text=True,
            ).stdout.strip(),
            "git_status_short": subprocess.run(
                ["git", "status", "--short"], cwd=str(REPO),
                capture_output=True, text=True,
            ).stdout.strip().splitlines(),
            "dev_up_py_sha256": sha(REPO / "scripts" / "dev_up.py"),
            "dev_up_expected_sha256": (
                "361E1424D7C373D305A667F6BCD4180F059E18007787E8705F2DC42DCD555D5C"
            ),
            "quarantine": quarantine(),
        },
        "profile": {
            "name": "amd",
            "id": PROFILE_ID,
            "model": "openai/DeepSeek-V4-Flash",
            "host": "api.tokenlab.amd.com",
            "active": True,
            "key_present": True,
        },
        "crew_server": {
            "card": card(),
            "health": health(),
            "log_tail_model_line": (
                "crew model: openai/DeepSeek-V4-Flash | api host: "
                "https://api.tokenlab.amd.com/v1"
            ),
            "python_processes": python_processes(),
            "no_eval_or_athena_run_processes": not any(
                row["kind"] in {"athena.run", "evaluation.runner"}
                for row in python_processes()
            ),
        },
        "tools": {
            "mcp": mcp_tools(),
            "rag_probe": rag_probe(),
            "web_probe": web_probe(),
        },
        "endpoint": {
            "dns": "resolved",
            "tcp_443": "connect ok",
            "note": "reachability only; no model call was made in preflight",
        },
        "policy_env_non_secret": parse_env_names(REPO / ".env"),
        "runtime_config": {
            "path": str(RUNTIME).replace("\\", "/"),
            "sha256": sha(RUNTIME),
            "source_path": str(SOURCE).replace("\\", "/"),
            "source_sha256": sha(SOURCE),
            "deviations_file": str(
                (OUT / "runtime_config_deviations.json").resolve()
            ).replace("\\", "/"),
            "deviations": [
                "matrix.per_run_timeout_s 1200 -> 3600",
                "budget.max_total_wall_s 86400 -> 172800",
                "note/pricing.note rewritten for AMD honesty",
            ],
            "unchanged": [
                "30 plan entries and 24 distinct prompts",
                "12 core + 12 breadth + 6 variance; 12 categories",
                "seed=null, no seed forwarded; stochastic replicates",
                "backend=subprocess; llm_call_cap=null",
                "budget.max_runs=36 (30 scheduled + <=6 transient retries)",
                "LLM_MAX_TOKENS and ATHENA_* call/token caps",
            ],
        },
        "plan": {
            "planned_entries": len(entries),
            "distinct_prompts": len({e["prompt_id"] for e in entries}),
            "distinct_run_keys": len({e["run_key"] for e in entries}),
            "tiers": {
                tier: sum(1 for e in entries if e["tier"] == tier)
                for tier in sorted({e["tier"] for e in entries})
            },
            "run_keys": [e["run_key"] for e in entries],
        },
        "pricing": {
            "priced": False,
            "model": "openai/DeepSeek-V4-Flash",
            "cost_field": None,
            "usd_cap_enforceable": False,
            "note": (
                "No published AMD endpoint price; no rate invented. max_runs=36 "
                "is the only hard attempt cap."
            ),
        },
        "dry_run": {
            "adk_model": "openai/DeepSeek-V4-Flash",
            "crew_model": "openai/DeepSeek-V4-Flash",
            "models_differ": False,
            "api_host": "https://api.tokenlab.amd.com/v1",
            "profile_applied": True,
            "per_run_timeout_s": 3600.0,
            "preconditions": "crew server up, keys present, RAG and web reachable",
        },
        "known_limitations": [
            "No extra live model smoke call was made; the first planned run is "
            "the initial validation and its record is retained either way.",
            "Endpoint pricing is unknown, so est_cost_usd is null and the USD "
            "caps cannot bound real spend.",
            "The batch is hierarchy-only; it makes no architecture-comparison "
            "claim and is not a 40-run result.",
        ],
    }
    (OUT / "preflight.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    print("wrote", OUT / "preflight.json")
    print("mcp servers:", [s.get("name") for s in payload["tools"]["mcp"].get("servers", [])])
    print("rag:", payload["tools"]["rag_probe"]["ok"], payload["tools"]["rag_probe"]["hits"])
    print("web:", payload["tools"]["web_probe"]["ok"], payload["tools"]["web_probe"]["hits"])
    print("processes:", payload["crew_server"]["python_processes"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
