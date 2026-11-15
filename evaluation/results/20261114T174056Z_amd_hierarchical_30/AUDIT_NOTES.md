# Independent audit findings — batch dispositions

Batch: `evaluation/results/20261114T174056Z_amd_hierarchical_30`
Plan: hierarchy-only, 30 scheduled keys, 24 distinct prompts (12 core + 12
breadth + 6 variance repeats), 12 categories.
Profile: `amd` (`367f57c4-d107-447c-a857-632fa8e6a454`), model
`openai/DeepSeek-V4-Flash`, host `api.tokenlab.amd.com`.
No source code, scoring rule, critic threshold, token cap, or guardrail was
changed. `scripts/dev_up.py` retains the user edit; SHA-256 is still
`361E1424D7C373D305A667F6BCD4180F059E18007787E8705F2DC42DCD555D5C`.
`evaluation/quarantine/` is untouched.

## 1. Parent provenance is not fixed by `--accounts amd` alone

The launcher loads the saved `amd` profile from the existing encrypted vault,
exports `LLM_API_KEY`, `LLM_MODEL`, `LLM_API_BASE`, `OPENAI_API_KEY`,
`OPENAI_API_BASE`, and `OPENAI_BASE_URL` into the **runner parent** before
`resolve_provenance()` runs, and the CrewAI server was activated on `amd`.
Verified as booleans only: `llm_model_is_amd=True`,
`llm_api_base_is_amd=True`, `openai_key_equals_amd=True`,
`openai_base_is_amd=True`. `run_meta.json` records `adk_model == crew_model ==
openai/DeepSeek-V4-Flash`, `models_differ=false`, `profile_applied=true`,
`profile_name=amd`, and both hosts `https://api.tokenlab.amd.com/v1`.

## 2. Effective hierarchical call cap: 12 → 40

`athena.config.caps.PATTERN_CAPS["hierarchical"]` is 40, but the historical
`.env` value `ATHENA_MAX_LLM_CALLS=12` (a Groq diagnostic leftover) overrode it.
The first pre-dataset pilot was aborted for exactly this reason. The corrected
launcher exports `ATHENA_MAX_LLM_CALLS=40` to the runner parent, every
`athena.run` child, and the restarted CrewAI server; the v2 config records
`matrix.llm_call_cap=40`. Effective value verified as 40. This is recorded in
`batch_policy.json` because `run_meta.json` does not carry the environment
override itself. Token/output caps, critic/security rules, and retry
classification are unchanged.

## 3. Timeout layers

The pilot under 300 s provider timeout / 240 s A2A timeout was aborted after
repeated LiteLLM timeout/retry banners and no stage progress. Endpoint health
`GET /v1/models` returned HTTP 200 in ~1.1 s and listed `DeepSeek-V4-Flash`, so
this was generation latency, not auth or reachability. Corrected layers:

| Layer | Setting |
|---|---|
| Provider call + role guard | `ATHENA_LLM_TIMEOUT_S=900` |
| A2A role call | `ATHENA_A2A_TIMEOUT_S=1200` |
| Per-run wall | 3600 s (config) |
| Batch wall | 172800 s (config) |

The aborted pilot's log and `run_meta.json` are preserved; see
`ABORTED_PILOT.json`. It produced no raw record and is not counted as one of the
30 scheduled-key attempts.

## 4. Offline commands

Only explicit batch-scoped commands are used:

```powershell
.venv\Scripts\python.exe -m evaluation.analyze --results evaluation/results/20261114T174056Z_amd_hierarchical_30
.venv\Scripts\python.exe -m evaluation.report --results evaluation/results/20261114T174056Z_amd_hierarchical_30 --out evaluation/results/20261114T174056Z_amd_hierarchical_30/REPORT_BATCH.md
```

No bare `evaluation.report` run is made; that would pool the older 75 attempts.

## 5. Generated interpretation is matrix-shaped

The generated narrative contains language about four patterns and small-group
caveats. This batch is **hierarchical only**: it cannot compare architectures.
That caveat is stated in the batch-specific narrative and the final report; the
generator source is not modified.

## 6. Volatile `cv-01` / `cv-02` answer keys

Both are `volatility=volatile`, `as_of=2025-01`, with a prose `facts` answer key
and empty `must_mention` / `numeric` lists. `cv-01` is scheduled twice and
`cv-02` once. Empty-key `answer_key_miss` outcomes are footnoted and excluded
from answer-key aggregate claims; scoring itself is unchanged.

## 7. Design description

Use the entry list, not `matrix.repeats`: 12 core + 12 breadth + 6 variance
(`repeat=1`), 24 distinct prompts, 12 categories (6 categories with 3 runs, 6
with 2). Selected-prompt personas: expert 13, novice 9, hurried 4, hostile 2,
manipulative 2. The variance tier is stochastic because no seed reaches the
provider.

## 8. Shared state paths

`BLACKBOARD_DB` and `EVENTS_DB` are exported as absolute paths under the repo
root for the runner parent and every child; the CrewAI server was restarted with
the same values. Evidence and token counts are checked per record for
inexplicable gaps.

## 9. Pricing

`openai/DeepSeek-V4-Flash` is unpriced on the AMD endpoint. Every
`est_cost_usd` stays `null`; the USD caps are inert. `max_runs=36` is the only
hard attempt cap (30 scheduled keys + at most 6 transient-retry attempts). No
AMD rate was invented.

## 10. Reporting denominators and integrity

- All-scored-attempt counts and valid-completed counts are reported separately.
- Security rates use evaluable denominators; a no-answer attempt is
  unevaluable, never a successful defence.
- Attempt IDs are unique; the 30 scheduled run keys are audited exactly.
- The dirty `scripts/dev_up.py` hash is recorded; the file is not staged or
  modified.
- Effective call cap is recorded manually in `batch_policy.json`.
