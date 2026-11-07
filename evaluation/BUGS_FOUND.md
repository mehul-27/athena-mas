# Evaluation-run bugs and environment findings

Findings from the live evaluation batches. B1-B4 come from the first batch
(`evaluation/results/20261106T194245Z/`). B5 is the defect that batch exposed:
its runs reported `completed` with zero evidence. B6 is the provider limit that
stopped the 2026-11-07 benchmark.

## Status summary

| ID | Title | Status |
|---|---|---|
| B1 | Recorded model is not the model the CrewAI stages ran on | **Fixed** — `adk_model`, `crew_model`, `provider`, `api_host`, `profile_applied`, `models_differ` recorded in `run_meta.json` and every record |
| B2 | Unbuffered stdout lost by `stream.reconfigure` | **Fixed** — `write_through=True` plus an explicit flush, and every `raw.jsonl` record is flushed as it is written |
| B3 | Per-run wall time dominated by the local token limiter | **Open (environment property, not a defect)** — the limiter is preserved deliberately |
| B4 | `--out` into an unwritable directory dies with a traceback | **Fixed** — returns `EXIT_INVALID` with one clear message and leaves no partial batch |
| B5 | Every run reported `completed` with zero evidence | **Fixed** — see below; this was the important one |
| B6 | Groq free-tier daily token quota stopped the 36-run benchmark | **Blocked externally** — 32 of 36 runs hit `TPD` limit 200000 |

---

## B5 - Every run in the first batch collected zero evidence (root cause)

**Severity:** critical (invalidated the whole batch)

**What happened.** All completed records in `20261106T194245Z/` reported
`terminal_state=completed` with `evidence_count=0`, `critic_status` rejected and
confidence 0. The run logs show why:

```
Error on session runner task: I/O operation on closed file
Agent web_researcher will run without the tools from toolset MCPToolset,
which failed to load: Failed to create MCP session: I/O operation on closed file
```

with a traceback ending in `subprocess.py`, line 1419:
`errwrite = msvcrt.get_osfhandle(stderr.fileno())` -> `ValueError: I/O operation
on closed file`.

**Root cause.** That batch used the harness's `in_process` backend, which wraps
each run in `contextlib.redirect_stdout/redirect_stderr` to a log file. ADK
binds `errlog=sys.stderr` as a default *at import time*, so MCP's stdio client
handed `subprocess` a file object whose handle was closed by the time a tool
server was spawned. No tool server started, ADK dropped the toolset, and every
research agent ran with no tools. The run still reported success because the
lost toolset was only logged, never recorded as a condition of the run.

This was **not** a search-layer defect. The one run in the 2026-11-07 batch that
executed before the quota ran out collected 7 real evidence items and 5
citations, which confirms the search and fetch path was healthy all along.

**Fix (verified).**
- `athena/tools/mcp_stdio.py` supplies a process-wide, never-closed stderr sink
  whose handle is proven usable before it is handed to MCP, so redirection or a
  replaced `sys.stderr` cannot invalidate the spawn.
- `athena/agents/toolset_health.py` records a failed toolset load as an error
  event and marks the run degraded instead of letting it look clean.
- `athena/summary.py` reads `evidence_count`/`citation_count` from the
  authoritative blackboard, so a summary can no longer disagree with the
  report that shipped. `degraded` now requires a real loss and always carries a
  reason.
- Tests: `tests/test_pipeline_toolset_health.py` (toolset survives
  `redirect_stdout`/`redirect_stderr`; a lost toolset is loud),
  `tests/test_pipeline_degradation.py` (an approved run with absorbed errors is
  not degraded; real losses are), `tests/test_pipeline_summary_evidence_count.py`.

**Evidence of the fix.** Hierarchical run `cafa1b22` and benchmark run
`gf-01__hierarchical__r0` both collected real evidence (6 and 7 items), produced
cited reports (3 and 5 citations resolving to captured sources) and were
approved by the critic.

## B5b - Malformed tool calls killed otherwise-healthy runs

**Severity:** high

The first batch also failed with
`tool call validation failed: parameters for tool fetch_page did not match
schema: missing properties: 'url'`, and later with a hallucinated tool name
(`webollar`). Either one ended the run even when evidence had already been
collected.

**Fix.** `athena/agents/tool_args.py` and `athena/config/tool_call_guard.py`
turn a provider-side tool-call validation failure into a bounded recovery: one
corrective retry that lists the permitted tool names and argument schemas, an
explicit `retry` event only when a retry actually happens, no invented URL, no
unbounded loop, and the run continues with the evidence it already has instead
of failing outright. Tests: `tests/test_pipeline_tool_call_rejection.py`.

---

## B6 - Groq free-tier daily token quota stopped the 36-run benchmark

**Severity:** blocking (external)

32 of the 36 records in `evaluation/results/20261107T133000Z/` failed with:

```
Rate limit reached for model `qwen/qwen3.8-27b` ... on tokens per day (TPD):
Limit 200000, Used 199534
```

One hierarchical run costs roughly 40,000-70,000 tokens, so the daily budget
supports about 3 runs. The batch therefore completed 1 run and failed 35. See
`evaluation/results/20261107T133000Z/BENCHMARK_OUTCOME.md`.

**This is not a code defect and was not worked around.** The rate limiter was
not disabled, the model was not switched mid-batch, and no result was
fabricated. Options are a larger provider tier, spreading the batch across
days with `--resume`, or a smaller plan.

---

## B1 - The recorded `model` is not the model three of the five stages ran on

**Severity:** medium (provenance, not correctness of the comparison)

**What happens.** `evaluation/runner.py` labels every run with
`LLM_MODEL` from the process environment (`groq/qwen/qwen3.8-27b` in `.env`), and
`run_meta.json` repeats that label. The A2A side is a *separate process* started
by `scripts/dev_up.py`, which merges the active settings profile
(`data/settings/active_profile.json` -> `data/settings/profiles.json`) into the
child environment. That profile sets a different model and gateway, so in one
run the ADK stages (planner/researchers) call `groq/qwen/qwen3.8-27b` while the
CrewAI stages (analyst/writer/critic) call `openai/deepseek/deepseek-v4.1-flash`
against `https://api.commandcode.ai/provider/v1`.

**Repro.**

```
.venv\Scripts\python.exe scripts\dev_up.py
.venv\Scripts\python.exe -m evaluation.runner --limit 1 --out .tmp\eval-smoke
type .tmp\eval-smoke\run_meta.json        # "model": "groq/qwen/qwen3.8-27b"
powershell -c "Get-Content .run\crew_a2a.log -Tail 40 | Select-String 'crew model'"
# crew model: openai/deepseek/deepseek-v4.1-flash | api host: https://api.commandcode.ai/provider/v1
```

**Impact.** A per-run report cannot be used to attribute results to one model.
The four patterns are still compared under identical conditions (every pattern
runs through the same split), so the pattern comparison stands, but the "Model
label" row in `REPORT.md` describes only the ADK half of the pipeline.

**Suggested fix (not applied - outside the editable surface).** Have the runner
resolve the active profile the same way `scripts/dev_up.py` does (or start the
crew server itself and record both labels), and record `adk_model` and
`crew_model` separately in `run_meta.json` and each record.

## B2 - `python -u -m evaluation.runner > log` produces an empty log until exit

**Severity:** low (operability)

`evaluation.runner.main()` calls `stream.reconfigure(encoding="utf-8")` on
stdout/stderr. On Windows that re-wraps the stream and drops the unbuffered mode
requested by `-u`, so the per-run progress lines only reach the redirected file
when the process exits. A long batch therefore looks dead while it is working.

**Repro.** `.venv\Scripts\python.exe -u -m evaluation.runner --limit 1 --out .tmp\x > .tmp\x.log 2>&1`
then watch `.tmp\x.log`: it stays at 0 bytes for the whole run, although
`.tmp\x/logs/<run_key>.log` and `raw.jsonl` show real progress.

**Suggested fix.** `stream.reconfigure(encoding=..., errors=..., write_through=True)`
(or `flush=True` in the default logger) in `main()`.

## B3 - Per-run wall time is dominated by the local token limiter, not the model

**Severity:** informational (it sets the achievable batch size)

`LLM_ITPM_LIMIT=7000` / `LLM_OTPM_LIMIT=1000` in `.env`, with a 60 s sliding
window in `athena/config/ratelimit.py`, produce exactly one model call per
minute for this workload. Measured on the first live run: 12 model calls,
71 027 input tokens and 25 186 output tokens over 857 s, with 13 `limiter_wait`
events. A 900 s `per_run_timeout_s` is therefore only ~5 % headroom, and a full
72-run plan cannot finish inside the 28 800 s `max_total_wall_s` budget cap:
~96 k tokens per run at 7 k tokens/minute is ~14 minutes per run, i.e. roughly
4 runs/hour.

This is a real property of the configured environment rather than a defect, but
it is the reason the batch below stopped where it did (see `REPORT.md`).

## B4 - `--out` into a non-writable directory dies with a raw traceback

**Severity:** low

`python -m evaluation.runner --out <some temp dir>` raises an uncaught
`PermissionError` from `Path.mkdir` before any record is written, instead of
reporting a bad output directory as `EXIT_INVALID`. Cost: one confusing failure
and no `run_meta.json` to explain it.

## Observations (not bugs)

* `gf-01__parallel__r0` (smoke) produced a zero-evidence gap report in 480 s:
  the parallel researchers made a single tool call, so the writer correctly
  refused to answer and the critic rejected. That is the designed
  "no evidence -> no invented answer" path, recorded as
  `terminal_state=completed`, `critic_status=rejected`, `confidence=0.0`.
* The sequential run in the smoke recorded three benign stage errors
  (`web_search: query must not be empty`, `fetch_page: HTTP 403`,
  `web_search: Blocked: this tool call was already made.`) and still produced a
  cited, critic-approved answer.
* With an unpriced model (`groq/qwen/qwen3.8-27b` has no entry in
  `data/pricing.json` on purpose), `evaluation.analyze` correctly skips
  `cost_by_pattern.png` and `median_cost_bar.png` and names the gap rather than
  plotting zeros. The cost charts therefore cannot exist for this model without
  inventing a rate.
