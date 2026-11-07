# Preflight — 30-run hierarchical benchmark

**Status: NOT APPROVED TO RUN.** Configuration, provider and pipeline checks pass,
but **capacity does not**. Details below, in the order the brief asked for.

Baseline: `main` @ `72aabda`, clean tree. Nothing in this report has been
committed, no benchmark run was executed, and no historical record was touched.

---

## A. Configuration

| Check | Result |
|---|---|
| Plan file | `evaluation/hierarchical_30.json` (new) |
| Entries | **30** (`target_runs` = 30) |
| Tiers | core=12, breadth=12, variance=6 |
| Distinct prompts | **24** (12 core + 12 breadth) |
| Categories | **12 / 12**, 2–3 runs each |
| Run keys | **30 unique** |
| Pattern | `hierarchical` only |
| Context sharing | `blackboard` only |
| Dataset IDs | all 24 validated against `evaluation/dataset.json` (48 items) |
| Existing 36-run plan | untouched (`evaluation/hierarchical_36.json`) |

Dry run (`python -m evaluation.runner --dry-run --config evaluation/hierarchical_30.json`):

```
plan      : hierarchical-30 - 30 runs from 24 prompts (scope=focused, target=30, min=30)
patterns  : hierarchical=30
tiers     : breadth=12, core=12, variance=6
categories: 12 present, 2-3 runs each
run keys  : 30 scheduled, 30 distinct
```

Note: `schema.MIN_PLAN_RUNS_FOCUSED` is exactly 30, so this plan sits on the
minimum. It validates, but any truncation would be rejected rather than silently
run — verified by the existing 29-entry rejection test.

---

## B. Provider

### Credentials found

| Source | Fingerprint (truncated SHA-256) | Organization |
|---|---|---|
| `.env` `LLM_API_KEY` | `f59a1c5492` | `org_01m36f32dfej585y7yvad40prh` |
| profile **Groq1** | `cbd1a14650` | `org_01m36f32dfej585y7yvad40prh` |
| profile **Groq2** | `66885c3173` | `org_01m6vtq65xem9a1xr4qxmc5dk2` |

All three are distinct key values. **Groq1 is in the same organization as the
`.env` key**; Groq2 is in a different one. No key value appears in this report,
in any record, or in any log — verified by scanning the produced JSONL.

### Model and endpoint consistency

| Property | Groq1 | Groq2 | Match |
|---|---|---|---|
| provider preset | groq | groq | yes |
| `api_base` | `https://api.groq.com/openai/v1` | same | yes |
| model | `groq/qwen/qwen3.8-27b` | same | yes |

`evaluation/accounts.py::verify_same_conditions` enforces this: a batch whose
accounts differ in endpoint, model, or credential identity is refused outright.
Both guards were exercised and both refused as designed.

### Measured limits

| Limit | Value | How measured |
|---|---|---|
| requests / minute | 1,000 | `x-ratelimit-limit-requests` header |
| tokens / minute | 8,000 | `x-ratelimit-limit-tokens` header |
| tokens / day | 200,000 | provider quota error text (observed in both orgs) |
| distinct daily budgets | **2** | different org ids, confirmed by forcing a 429 on each |

### Account usage policy — as implemented

- The account for each run is fixed **before the batch starts**, from a
  deterministic round-robin over sorted run keys (`accounts.allocation_for`).
  Result: 15 runs on Groq1, 15 on Groq2 for this plan.
- A retry of the same run key keeps the **same** account. An attempt is never
  split across credentials.
- A quota error is **never** used to re-choose an account. That would be
  rotation to evade enforcement, which the brief forbids and which the code
  makes structurally impossible: `account_for()` reads a precomputed mapping and
  never writes it.
- Every attempt records `account_label`, `account_fingerprint`, `account_id`.

### Capacity — **this is the blocker**

Measured cost of a *completed* hierarchical run (5 completed runs on record):

| statistic | tokens |
|---|---|
| min | 16,759 |
| **median** | **20,584** |
| mean | 22,396 |
| max | 31,471 |

| Scenario | 30 runs need | Available (2 × 200k) | Verdict |
|---|---|---|---|
| median run | 617,520 | 400,000 | **does not fit (1.54×)** |
| worst run | 944,130 | 400,000 | does not fit (2.36×) |
| runs affordable **today** | — | — | **19 at median size, 12 at worst** |

Complication that reduces effective capacity further: the CrewAI half
(analyst/writer/critic) runs on the **`.env` key**, which is in **Groq1's
organization**. So:

- a **Groq2**-assigned run: ADK half bills ORG2, CrewAI half still bills ORG1;
- a **Groq1**-assigned run: both halves bill ORG1.

Groq1's budget is therefore consumed by roughly 15 ADK halves **plus all 30
CrewAI halves**, making it the binding constraint well before the naive 400,000
figure.

**Conclusion: 30 planned runs cannot complete in one quota day.** They would need
roughly 2 days at the measured median, or more.

---

## C. Pipeline health

Verified by a diagnostic run (`gf-01__hierarchical__r0`, excluded from the
benchmark and deleted after inspection):

| Check | Result |
|---|---|
| A2A server up | yes (`/agent-ack` card 200) |
| MCP toolsets load, researchers get tools | yes — `model_calls=1`, `tool_calls=1` |
| Evidence written to Blackboard | not reached in this attempt (quota cut it off) |
| Citations captured | not reached |
| Critic evaluation | not reached |
| Account attribution recorded | **yes** — `Groq1#cbd1a14650` |
| Infrastructure classification correct | **yes** — `infrastructure` / `provider_quota` / `after_model` |
| Secret material in record | **none** |

The one attempt reached the model (707 in / 100 out tokens), then the provider
returned a quota error. That is correctly classified as infrastructure, not as a
research failure — which is exactly the distinction added in the previous commit
working as intended.

**One real bug was found and fixed during preflight:** `Account.public()` wrote
`api_base` and `model` into the record, which the record schema forbids
(`extra="forbid"`). Every run would have failed validation. Fixed before any
benchmark run; the fix is why the second diagnostic attempt produced a valid
record.

### Pipeline health is therefore PARTIAL

Evidence → citation → critic was never exercised end-to-end in this preflight,
because the attempt was cut off before the research stage completed. Earlier
work (commit `7f39047` and the `20261107T133000Z` batch) demonstrates that path
works — one run recorded 7 evidence items and 5 citations — but **it was not
re-verified today**.

---

## D. Runner

| Check | Result |
|---|---|
| Subprocess backend | yes (the only supported live path) |
| Timeout kills the child | yes — `process.kill()` after `wait(timeout)`; covered by tests |
| Resume | yes — `--resume` default, skips completed keys |
| JSONL flush per attempt | yes (B2 fix) |
| Duplicate completed records | prevented by resume keyed on `run_key` |
| Infrastructure classification | yes, verified above |
| Per-run account env override | **added this session**, works |

---

## Smoke test

**One** diagnostic run, in `evaluation/results/diagnostic_smoke_20261107/`,
**deleted after inspection**. It is not part of the 30 and contributed no
benchmark record.

Outcome: `infra_error` / `provider_quota` / `after_model` in 79 s. It failed on
provider quota after the model had answered once — at the time my own
concurrent quota probes had saturated the per-minute window. Both accounts now
report full daily headroom (HTTP 200, 7,985/8,000 remaining per minute).

---

## What I need from you before running

1. **Accept a multi-day batch, or reduce scope.** At the measured median, 30
   completed runs need ~618k tokens against 400k/day across two orgs, and
   Groq1's sharing with the CrewAI server narrows that further. Options:
   - run ~12–15 today and resume tomorrow with `--resume` (the harness supports
     this and never double-records);
   - or approve a smaller plan (e.g. 18 runs) that fits one day;
   - or supply a paid tier / higher limit for either account.
2. **Confirm the `.env` key may keep serving the CrewAI half.** It is a third
   credential in Groq1's org. If both authorized accounts should own both halves,
   the CrewAI server must be restarted per account, which conflicts with
   "do not switch accounts during an active run" unless assignment is per-run —
   and it currently cannot be, because the server is one long-lived process.
3. **Confirm the provider permits this arrangement.** Nothing I measured
   distinguishes a legitimately separate second account from any other second
   key; the two are in different organizations and both respond normally. I can
   verify technical behaviour, not your agreement with the provider.

I have not started the benchmark. Say which option you want and I will run it,
produce both reports, and commit.
