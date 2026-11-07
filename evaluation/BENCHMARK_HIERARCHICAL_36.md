# Athena MAS — hierarchical research benchmark (36 runs)

Focused benchmark configuration and the run-level notes that belong with it.
Companion to `evaluation/REPORT.md` (all batches) and `evaluation/BUGS_FOUND.md`.

## Configuration

`evaluation/hierarchical_36.json`

| Field | Value |
|---|---|
| Plan | 36 entries, `hierarchical` only, `blackboard` context sharing |
| Tiers | 12 core, 12 breadth, 12 variance |
| Distinct prompts | 24 (12 categories x 3 runs each) |
| Backend | `subprocess` (a timeout really kills the child) |
| Per-run timeout | 1200 s |
| Attempt cap | 40 (36 planned + up to 4 transient-retry attempts) |
| Budget | 8.00 USD total, 0.60 USD per run |

The variance tier repeats each core prompt once with `repeat=1`. Athena does not
forward a seed to the provider, so these are **stochastic replicates**, not
seeded reproductions. The configuration says so explicitly rather than
implying determinism it does not have.

## Preconditions (verified before the batch)

1. CrewAI A2A server answering on `127.0.0.1:9001`.
2. One provider for **both** halves of the pipeline. The ADK half reads
   `LLM_MODEL`/`LLM_API_KEY`; the CrewAI half is started by `scripts/dev_up.py`,
   which merges the active settings profile and can therefore point at a
   different model and endpoint. A profile pointing at a provider without API
   access is a silent failure mode for the analyst/writer/critic stages, so the
   active profile must be either absent or identical to the ADK target. The
   runner records `adk_model`, `crew_model`, `provider`, `api_host` and
   `profile_applied` in `run_meta.json`.
3. Web search reachable. DuckDuckGo is primary; SearXNG on
   `http://127.0.0.1:8080` is the configured fallback (`ATHENA_SEARXNG_URL`).
4. RAG corpus present (`data/corpus`, Chroma at `data/chroma`) for `dr-*` prompts.
5. Output directory writable, event and blackboard stores writable.
6. Pricing: the active model is **unpriced** in `data/pricing.json`. No rate is
   invented for it. `--acknowledge-unpriced` is required and is recorded in
   `run_meta.json`; the USD cap is therefore not an enforceable spend limit.

## Commands

```powershell
# terminal 1 — CrewAI A2A server
$env:ATHENA_SEARXNG_URL = 'http://127.0.0.1:8080'
.venv\Scripts\python.exe scripts\dev_up.py

# terminal 2 — dry run, then the batch
.venv\Scripts\python.exe -m evaluation.runner --dry-run --config evaluation/hierarchical_36.json
.venv\Scripts\python.exe -m evaluation.runner --config evaluation/hierarchical_36.json `
    --backend subprocess --acknowledge-unpriced --out evaluation/results/<UTC timestamp>

# analysis
.venv\Scripts\python.exe -m evaluation.analyze --results evaluation/results/<UTC timestamp>
.venv\Scripts\python.exe -m evaluation.report
```

## Batch separation

`evaluation/results/20261106T194245Z/` is the earlier six-attempt batch. It is
kept for provenance and is **not** part of this benchmark. Its runs reported
`terminal_state=completed` with zero evidence because MCP stdio tool servers
failed to start under process-wide stream redirection — see
`evaluation/BUGS_FOUND.md` (B5). Never combine its numbers with this batch.

## What makes a run successful

Reaching `terminal_state=completed` is not sufficient. A run counts as a
success only when the scorer's rubric passes, which requires at minimum a
non-empty answer, evidence actually recorded on the blackboard, and citations
that resolve to captured evidence. A completed run with zero evidence, a
rejected critic verdict or a low rubric score is reported as such.
