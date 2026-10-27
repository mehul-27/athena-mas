"""Preflight: which host would each LLM call path reach, and does it work?

Dry run by default — it builds the same model objects the pipeline builds and
reports where they point. With ``--live`` it makes one tiny call per path.

Never prints a key or a full URL: hostnames only, and the key only as a length.

    python scripts/preflight.py
    python scripts/preflight.py --live
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from athena.config import llm  # noqa: E402
from athena.config.endpoint_guard import expected_host, guard_active  # noqa: E402
from preflight_probes import (  # noqa: E402
    PathInfo,
    ProbeResult,
    describe_paths,
)

HEADER = f"{'path':<20} {'host':<24} {'model':<34} detail"


def key_status() -> str:
    key = llm._require_api_key()
    return f"present (length {len(key)})"


def table(paths: list[PathInfo], wanted: str, active: bool) -> tuple[str, bool]:
    """The dry-run view; returns the text and whether any host is wrong."""
    lines = [HEADER, "-" * len(HEADER)]
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


def result_line(result: ProbeResult) -> str:
    return (
        f"  {result.path:<20} {result.status():<44} host "
        f"{(result.host or '(default)'):<22} tokens "
        f"{result.tokens_in}/{result.tokens_out} reasoning "
        f"{result.reasoning_tokens} calls {result.calls}"
    )


def collect_live(probes) -> tuple[list[ProbeResult], list[str]]:
    """Run every probe, even after a failure. No printing."""
    from preflight_probes import failure

    results: list[ProbeResult] = []
    models: list[str] = []
    for path, probe in probes:
        models.append(path.model)
        try:
            results.append(probe())
        except Exception as exc:  # noqa: BLE001 - a probe must never crash the run
            results.append(failure(path.name, path.host, exc))
    return results, models


def exit_code(results: list[ProbeResult]) -> int:
    return 0 if all(not result.failed for result in results) else 1


def as_json(results: list[ProbeResult], models: list[str]) -> list[dict]:
    """Per-path results, redacted: no key, no full URL, nothing else on stdout."""
    payload = []
    for result, model in zip(results, models):
        payload.append({
            "path": result.path,
            "ok": not result.failed,
            "host": result.host,
            "model": model,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "reasoning": result.reasoning_tokens,
            "calls": result.calls,
            "error_type": result.error_type,
            "error": result.error_message,
        })
    return payload


def run_live(probes) -> int:
    """Run every probe, even after a failure, and summarise."""
    print("\nlive probe:")
    results, _models = collect_live(probes)
    for result in results:
        print(result_line(result))

    ok = sum(1 for result in results if not result.failed)
    print(f"\n{ok} of {len(results)} paths ok")
    bad = [result.path for result in results if result.failed]
    if bad:
        print("failed: " + ", ".join(bad))
    return exit_code(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="preflight")
    parser.add_argument("--live", action="store_true",
                        help="make one tiny real call per path")
    parser.add_argument("--json", action="store_true",
                        help="print one JSON list of per-path results and nothing else")
    args = parser.parse_args(argv)

    llm.configure_process()
    wanted = expected_host()
    active = guard_active()
    built = describe_paths()

    if args.json:
        if not args.live:
            print(json.dumps({"error": "--json needs --live"}))
            return 2
        results, models = collect_live(built)
        print(json.dumps(as_json(results, models), indent=2))
        return exit_code(results)

    text, mismatch = table([info for info, _ in built], wanted, active)
    print(text)
    print(f"api key       : {key_status()}")

    if not args.live:
        print("\ndry run: no calls made (pass --live to make one per path)")
        return 1 if mismatch else 0

    return run_live(built) or (1 if mismatch else 0)


if __name__ == "__main__":
    raise SystemExit(main())
