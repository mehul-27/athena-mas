# Evaluation-run bugs and environment findings

Findings from the live evaluation batch (started 2026-11-06T19:42:55Z, results in
`evaluation/results/20261106T194245Z/`). Nothing under `athena/` was modified.

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
