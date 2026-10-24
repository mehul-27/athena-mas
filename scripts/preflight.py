"""Preflight: which host would each LLM call path actually reach?

Dry run by default — it builds the same model objects the pipeline builds and
reports where they point. With ``--live`` it makes one tiny call per path.

Never prints a key or a full URL: hostnames only, and the key only as a length.

    python scripts/preflight.py
    python scripts/preflight.py --live
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena.config import llm  # noqa: E402
from athena.config.endpoint_guard import expected_host, guard_active, host_of
from athena.observability.litellm_usage import install_usage_logger, usage_scope  # noqa: E402

PROMPT = "Say OK"
MAX_TOKENS = 300


@dataclass
class Path:
    name: str
    host: str
    model: str
    detail: str


def _adk() -> tuple[Path, object]:
    model = llm.get_adk_model("planner")
    base = model._additional_args.get("api_base") or ""
    return (
        Path("adk-lite-llm", host_of(base), model.model,
             "api_base passed explicitly"),
        model,
    )


def _crew_plain() -> tuple[Path, object]:
    client = llm.get_crewai_llm("analyst")
    return (
        Path("crewai-plain", host_of(client.base_url or ""), client.model,
             "base_url + api_base passed explicitly"),
        client,
    )


def _crew_structured() -> tuple[Path, object]:
    """CrewAI's structured path cannot take api_base; it reads the env."""
    client = llm.get_crewai_llm("writer")
    env_base = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or ""
    return (
        Path("crewai-structured", host_of(env_base), client.model,
             "env OPENAI_BASE_URL (InternalInstructor passes no api_base)"),
        client,
    )


BUILDERS = (_adk, _crew_plain, _crew_structured)


def collect() -> list[tuple[Path, object]]:
    return [builder() for builder in BUILDERS]


def key_status() -> str:
    key = llm._require_api_key()
    return f"present (length {len(key)})"


def report(paths: list[Path], wanted: str, active: bool) -> tuple[str, bool]:
    """Render the dry-run table; return it with a mismatch flag."""
    header = f"{'path':<20} {'host':<24} {'model':<34} detail"
    lines = [header, "-" * len(header)]
    mismatch = False
    for path in paths:
        shown = path.host or "(provider default)"
        if wanted and path.host != wanted:
            shown += "  <- MISMATCH"
            mismatch = True
        lines.append(f"{path.name:<20} {shown:<24} {path.model:<34} {path.detail}")
    lines.append("")
    lines.append(f"expected host : {wanted or '(LLM_API_BASE not set)'}")
    lines.append(f"endpoint guard: {'active' if active else 'inactive'}")
    return "\n".join(lines), mismatch


async def _live_adk(model) -> dict:
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    request = LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text=PROMPT)])],
        config=types.GenerateContentConfig(max_output_tokens=MAX_TOKENS),
    )
    tokens = {"in": 0, "out": 0, "reasoning": 0}
    async for response in model.generate_content_async(request):
        usage = getattr(response, "usage_metadata", None)
        if usage:
            tokens["in"] = getattr(usage, "prompt_token_count", 0) or 0
            tokens["out"] = getattr(usage, "candidates_token_count", 0) or 0
            tokens["reasoning"] = getattr(usage, "thoughts_token_count", 0) or 0
    return {"status": "ok", **tokens}


def _live_crew(client, structured: bool, role: str) -> dict:
    """One tiny call, with usage read at the litellm layer."""
    with usage_scope(role) as scope:
        if structured:
            from pydantic import BaseModel

            class Probe(BaseModel):
                text: str

            raw = client.call(PROMPT, response_model=Probe)
        else:
            raw = client.call(PROMPT)
    capturable = scope.tokens_in or scope.tokens_out or scope.calls
    status = f"ok ({len(str(raw))} chars)"
    if not capturable:
        status += " USAGE NOT CAPTURED"
    return {"status": status, **scope.as_dict()}


def run_live(kind: str, client) -> dict:
    try:
        if kind == "adk-lite-llm":
            return asyncio.run(_live_adk(client))
        return _live_crew(client, structured=kind == "crewai-structured", role=kind)
    except Exception as exc:  # noqa: BLE001 - the point is to report it
        return {"status": f"{type(exc).__name__}", "in": 0, "out": 0, "reasoning": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="preflight")
    parser.add_argument("--live", action="store_true",
                        help="make one tiny real call per path")
    args = parser.parse_args(argv)

    llm.configure_process()
    install_usage_logger()
    wanted = expected_host()
    active = guard_active()
    built = collect()

    text, mismatch = report([path for path, _ in built], wanted, active)
    print(text)
    print(f"api key       : {key_status()}")

    if not args.live:
        print("\ndry run: no calls made (pass --live to make one per path)")
        return 1 if mismatch else 0

    print("\nlive probe:")
    failed = False
    for path, client in built:
        result = run_live(path.name, client)
        status = result["status"]
        if status != "ok" and not status.startswith("ok ("):
            failed = True
        if "USAGE NOT CAPTURED" in status:
            failed = True
        print(
            f"  {path.name:<20} {status:<34} host {path.host or '(default)':<22}"
            f" tokens {result['tokens_in']}/{result['tokens_out']} "
            f"reasoning {result['reasoning_tokens']} calls {result['calls']}"
        )
    return 1 if (failed or mismatch) else 0


if __name__ == "__main__":
    raise SystemExit(main())
