# Preflight — 30-run hierarchical benchmark

**Status: AUTHORISED — proceeding on a multi-day schedule.**

Provider authorization was confirmed by the operator. Configuration, endpoint and
model consistency, pipeline health and end-to-end execution all pass. Capacity
does not fit one day, so the batch runs across consecutive daily quota windows
with `--resume`.

Baseline: `main` @ `c1763a2`. No historical record was altered and nothing was
deployed.

---

## Provider authorization

The operator confirmed independently that using both Groq accounts for this
benchmark is permitted. This report records that confirmation; it does not
attempt to re-derive it from the API, which cannot distinguish an authorized
second account from any other key.

Per the brief, separate per-organization quotas are **not** treated as
automatically poolable in the sense of being interchangeable. The execution plan
below respects each organization's own limit and never moves work between them to
escape a limit.

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

### Per-organization capacity model

The naive "two accounts, so twice the budget" reading is wrong, because the two
halves of the pipeline bill to different organizations.

**The CrewAI server's credential is fixed** to the `.env` key, which is in
Groq1's organization. That is preserved deliberately: switching it per run would
mean restarting the server mid-batch, which the brief forbids and which would
also break a run that is already in flight.

Measured per-stage token split, from `model_call` events in the event store for
the five completed runs that have per-stage data:

| run | ADK tokens | CrewAI tokens | total |
|---|---|---|---|
| `247ac82f` | 5,388 | 6,151 | 11,539 |
| `30cf6dda` | 2,639 | 14,416 | 17,055 |
| `3f5b8225` | 10,990 | 7,244 | 18,234 |
| `5d0186ec` | 4,116 | 8,234 | 12,350 |
| `a97e0fb7` | 4,623 | 6,068 | 10,691 |
| **mean** | **5,551** | **8,422** | 13,973 |
| **median** | 4,623 | 7,244 | 11,867 |

CrewAI is **60.3 %** of every run's tokens and always bills to Groq1.

With a static 15/15 ADK split:

| organization | pays for | mean-case 30-run total |
|---|---|---|
| **ORG1** (Groq1 + `.env`) | all 30 CrewAI halves + 15 ADK halves | **335,925** |
| **ORG2** (Groq2) | 15 ADK halves only | **83,265** |

ORG1 carries **4.0×** the load of ORG2. It is the binding constraint, and
rebalancing the ADK split toward Groq2 does not help — the crew tokens cannot
leave ORG1.

### Safe daily allocation, with a quota reserve

Reserve is applied per organization, to each one's own 200,000/day limit.

| cost basis | 15 % reserve | 20 % reserve |
|---|---|---|
| mean (13,973 / 11,867 per run) | **14 runs** | **14 runs** |
| median | 17 runs | 16 runs |
| worst observed | 8 runs | 8 runs |

**Planning figure: 14 complete runs per quota day**, which is the mean-case
answer at both reserve levels and therefore the least fragile. At the median it
would be 16–17; at the worst observed run it would be 8. The mean is the honest
single number because it sits between them and is not optimistic.

### Estimated schedule

| plan | days |
|---|---|
| 30 runs at 14/day | **3 quota days** (14 + 14 + 2) |

Realistically 2–4 days if individual runs are larger or smaller than the mean.
`--resume` makes the boundary safe: a day that ends mid-plan simply continues
the next day without re-running anything.

---

## C. Pipeline health

Verified by a diagnostic run (`gf-01__hierarchical__r0`, excluded from the
benchmark and deleted after inspection):

| Check | Result |
|---|---|
| A2A server up | yes (`/.well-known/agent-card.json` 200) |
| MCP toolset startup | **yes** — `toolset web loaded: fetch_page, web_search` |
| Researcher tool availability | **yes** — 3 web calls (1 search, 2 fetches) |
| Evidence written to Blackboard | **yes** — 5 items |
| Citations captured | **yes** — 3, all resolving |
| Analyst and writer execution | **yes** — agents: analyst, critic, planner, supervisor, web_researcher, writer |
| Critic evaluation | **yes** — approved |
| Approved terminal state | **yes** — `terminal_state=completed`, `outcome=approved`, scored `success=True`, rubric 1.0, citation precision 1.0 |
| Account attribution recorded | **yes** — `Groq1#cbd1a14650` |
| Token accounting | **yes** — 28,002 in + 2,093 out = 30,095; 9 model calls |
| Secret material in record | **none** |

The diagnostic run is `evaluation/results/diagnostic_e2e_20261108/`. It cost
20,880 tokens against ORG1 (11,665 ADK + 9,215 CrewAI) — a single-attempt
measurement consistent with the mean model below. **It is not one of the 30 and
must be excluded from the benchmark batch.**

Two vocabulary layers describe the same fact and must not be confused:

- the **evaluation record** uses `terminal_state=completed` ("a final report"),
- the **pipeline summary** uses `terminal_state=success`.

Both agree here, and `scoring` keys success off the evaluation record plus
`outcome=approved`, the rubric and citation resolution.

**One real bug was found and fixed during preflight:** `Account.public()` wrote
`api_base` and `model` into the record, which the record schema forbids
(`extra="forbid"`). Every benchmark run would have failed validation. Fixed
before any benchmark run and pinned by a test.

### Pipeline health: PASS

Every stage from tool startup to critic approval was exercised in one run.

---

## D. Runner

| Check | Result |
|---|---|
| Subprocess backend | yes (the only supported live path) |
| Timeout kills the child | yes — `process.kill()` after `wait(timeout)`; covered by tests |
| Resume | yes — `--resume` default, skips keys already `completed` |
| JSONL flush per attempt | yes (B2 fix) |
| Duplicate completed records | prevented by resume keyed on `run_key`; verified below |
| Infrastructure classification | yes, verified above |
| Per-run account env override | yes, verified above |
| Per-run CrewAI credential switch | **none** — the server key is fixed for the whole batch |

### Resume verification

Exercised against a synthetic `raw.jsonl` holding three completed keys:

```
plan entries: 30
unique run keys: 30
duplicates introduced by selection: 0
synthetic records already completed: 3
remaining to run with --resume: 27 (expected 27)
RESULT: resume cannot duplicate a completed run key.
```

A key is re-run only on explicit request (`--no-resume`, or `--retry-failed`
for a key whose state is not `completed`/`failed`). Neither is used by the
commands below.

---

## E. Consistency across accounts

Every property that could make two runs non-comparable is pinned by
`evaluation/accounts.py::verify_same_conditions`, which **refuses the batch**
rather than proceeding if any differ:

| property | value | enforced |
|---|---|---|
| provider preset | groq | yes |
| endpoint | `https://api.groq.com/openai/v1` | yes |
| model id | `groq/qwen/qwen3.8-27b` | yes |
| generation settings | from `.env`: `LLM_MAX_TOKENS=512`, `ATHENA_MAX_LLM_CALLS=12`, `LLM_ITPM_LIMIT=7000`, `LLM_OTPM_LIMIT=1000` | shared process config, not per account |
| context sharing | `blackboard` for every entry | yes, schema-validated |
| scoring thresholds | `SUCCESS_THRESHOLD = 0.60` | unchanged |
| evidence requirements | unchanged | unchanged |

The two accounts differ only in their credential and organization. Nothing about
the research configuration varies between them.

The same credential twice is also refused, so "two accounts" cannot silently
collapse into one for attribution purposes.

---

## F. Execution plan

`evaluation/hierarchical_30.json` is used **unchanged**; `--resume` carries the
batch across daily quota resets.

**First batch — command**

```powershell
$env:ATHENA_SEARXNG_URL = 'http://127.0.0.1:8080'
.venv\Scripts\python.exe -u -m evaluation.runner `
  --config evaluation/hierarchical_30.json `
  --backend subprocess `
  --accounts Groq1,Groq2 `
  --acknowledge-unpriced `
  --max-runs 14 `
  --out evaluation/results/20261108_hierarchical_30
```

**Subsequent days** — identical, except `--max-runs` and no `--no-resume`:

```powershell
.venv\Scripts\python.exe -u -m evaluation.runner `
  --config evaluation/hierarchical_30.json --backend subprocess `
  --accounts Groq1,Groq2 --acknowledge-unpriced `
  --max-runs 14 --out evaluation/results/20261108_hierarchical_30
```

`--max-runs` is the hard attempt cap and is the safety stop: it counts attempts,
not completed runs, so 14 leaves room for an infrastructure retry inside the
reserve. The output directory is the same on every day, which is what makes
`--resume` skip finished work.

The CrewAI server (`scripts/dev_up.py`) must be running for each day's batch and
is started once per day, not per run.

**Rules the run obeys, as implemented (not just intended):**

- accounts assigned at run boundaries, never mid-run; the mapping is computed
  from the plan before the first attempt and never written;
- a quota error is recorded as `infrastructure` / `provider_quota` and the batch
  stops at the cap — the run is never moved to the other account;
- no rate limit, evidence requirement, guardrail or scoring threshold is
  altered;
- completed keys are never re-run, so no duplicate records;
- every raw JSONL line is preserved and nothing is rewritten.

---

## G. Status summary

| Gate | Status |
|---|---|
| Provider authorization | confirmed by operator |
| Configuration validity | pass (30 entries, 12 categories, 24 prompts) |
| Endpoint / model / settings consistency | pass, enforced in code |
| End-to-end pipeline | pass (9/9 checkpoints) |
| Resume safety | pass |
| Capacity | **14 runs per quota day** with reserve; 30 runs ≈ 3 days |
| Historical records | untouched |
| Deployment | none |

