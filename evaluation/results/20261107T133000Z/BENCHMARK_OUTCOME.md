# Benchmark outcome — 2026-11-07 (hierarchical, 36-run plan)

**Status: the benchmark did not complete. 1 of 36 planned runs produced a valid
research result. The remaining 35 failed against the provider's daily token
quota, not against any defect in the pipeline.**

This file records what actually happened so that
`evaluation/results/20261107T133000Z/REPORT.md` is never read as a completed
36-run benchmark.

## What ran

| Item | Value |
|---|---|
| Batch | `evaluation/results/20261107T133000Z/` |
| Plan | `hierarchical_36.json` — 36 entries, hierarchical only, blackboard sharing |
| Backend | `subprocess` |
| Per-run timeout | 1200 s |
| Model (both halves) | `groq/qwen/qwen3.8-27b`, no profile override (`profile_applied: false`) |
| Records written | 36 |
| Completed with a valid result | **1** |
| Failed | **35** |

## Why 35 runs failed

32 records carry `RateLimitError`. The provider's response is explicit:

```
Rate limit reached for model `qwen/qwen3.8-27b` ... service tier `on_demand`
on tokens per day (TPD): Limit 200000, Used 199534
```

The Groq free tier allows **200,000 tokens per day** for this model. A single
hierarchical run consumes roughly 40,000-70,000 tokens (planner, researcher,
analyst, writer, critic, plus recovery). The first two runs exhausted the daily
budget; every later run failed at its first model call, which is why those
records are 30-100 s long instead of ~10 minutes.

The remaining three failures were incidental to the same period: one analyst
error, one `fetch_page` failure, one A2A client timeout.

A retry batch was started after a quota check returned 200. It failed
immediately with the same `TPD` message (199,534 of 200,000 used), so it was
stopped rather than left to burn attempts. That batch directory was removed
because it contained no data.

Nothing here was fabricated or padded. The 35 failed records are real records of
runs that could not reach a model.

## What the one valid run shows

`gf-01__hierarchical__r0` (element with symbol W):

| Metric | Value |
|---|---|
| Outcome | approved |
| Terminal state | success |
| Evidence items | 7 |
| Citations | 5 |
| Critic | approved |
| Wall time | 679 s |
| Degraded flag | true (a tool call was absorbed; the reason is recorded) |

That run demonstrates the pipeline end to end: the planner produced executable
steps, the MCP tool servers loaded, web search and fetch produced citable
evidence on the blackboard, the writer produced a cited report, and the critic
approved it. An earlier equivalent pilot (`cafa1b22`) recorded 6 evidence items
and 3 citations in 721 s.

## What is required to finish the benchmark

One of:

1. **A provider tier with a larger daily budget** for the active model, or
2. **Spread the batch across days** — roughly 3 runs/day at the current quota,
   which makes 36 runs a ~12-day exercise, or
3. **A smaller plan** that fits one day's quota (about 3-4 runs), reported as
   such rather than presented as 36.

The harness supports all three: `--resume` continues a batch without
re-running completed keys, `--retry-failed` re-runs only the failed ones, and
`--max-runs` bounds a single day's spend.

## Honest reading of the numbers

`REPORT.md` reports `1/36 (3%)` success and a `not_completed` failure category
for 35 runs. That is the correct arithmetic for this batch and it must not be
quoted as a measure of Athena's research quality: 35 of those runs never
executed the research pipeline at all because no model call succeeded.

The valid n for research-quality questions from this batch is **1**, plus the
earlier pilot — far too small to compare patterns, categories or prompts.
The pattern comparison this batch was designed for (hierarchical vs sequential
vs parallel vs p2p) is therefore **not answered**.
