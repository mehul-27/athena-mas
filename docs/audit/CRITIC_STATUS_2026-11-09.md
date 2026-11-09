# Critic rejection status — 2026-11-09

> Follow-up: the P2P gaps G1-G4 identified in this review are now fixed. The original review below is retained as a historical checkpoint; see the implementation follow-up at the end for current status and verification.

## Scope and method

Read-only review of the current dirty tree (branch `main`, HEAD
`cad3916`, all uncommitted changes preserved). I changed no application source,
test, configuration, threshold, raw-data, quarantine, benchmark-plan, or
`scripts/dev_up.py` file. No commit, deployment, benchmark, Athena inference,
provider probe, app server, or external network call was made. The only file I
added is this report; the tree also contains concurrent parent work noted at the
end.

Historical records were read only to identify rejection root causes. Failed
infrastructure attempts were not treated as quality evidence. The working tree
also changed during this review: `evaluation/runner.py` gained the separately
authorized per-agent token aggregation. That change is noted at the end and was
not part of the critic test run.

## Verdict

- The specific deterministic, prompt and critic-context defects behind the known
  **standard-path** false rejections are **fixed in the current dirty tree**, and
  the focused fake-only regressions pass. P2P still has the parity gaps below;
  this is not a claim that every critic path is fully fixed.
- The live critic's acceptance rate is **not measured**. No live model/provider
  call and no fresh representative sample were run, so this report does not
  claim the historical rejection concern is empirically solved.
- The historical zero-evidence gap answers were rejected correctly. The fixes
  target false rejections of supported or evidence-qualified reports; they do
  not turn generic rejections into approvals.

## Call paths in the current tree

- **Standard (blackboard):** supervisor -> `build_a2a_stage("critic")` ->
  `crew_server.run_role_request` -> `ROLES["critic"]` (`crew_roles.critique`) ->
  prompt containing the assessment coverage, `critic_evidence_text`, the full
  `FinalReport` JSON and the task finding requirement -> `crew_call` ->
  `calibrate(..., assessment, evidence_texts)` -> blackboard `critique`.
- **P2P (inline context):** `P2PAgent._call_role("critic")` -> `p2p_critique` ->
  `CRITIC_PROMPT` plus `P2PContext.for_role("critic")` (REPORT, ANALYSIS, plan,
  status/coverage, eight-item evidence) -> `crew_call` -> `_audit_critique` ->
  `calibrate(..., assessment)` with no `evidence_texts` -> blackboard `critique`.
  The blackboard stores the calibrated verdict; the stage event carries the raw
  model content.
- **Recovery:** sequential/parallel run the post-critic `RecoveryReviewAgent`
  when evidence recovery is needed or the critique severity is high;
  hierarchical loops over evidence recovery and high-severity prose revision;
  P2P runs one assessment-driven research/role pass and does not consult the
  critique severity.

## What is fixed in the current tree

### 1. Generic evidence-gap wording no longer forces rejection

`athena/agents/critique_rules.py:32-58` now separates direct admissions
(`question`, `answer`) from context words (`definition`, `evidence`,
`handbook`). Context words block only when the assessment says the requested
answer was not produced and a qualified partial answer is not possible. Direct
admissions remain rejection triggers. `calibrate` at `:61-124` uses the run's
`answer_as_asked` and `qualified_possible`; it only converts approval to
rejection, never the reverse.

Covered by `tests/test_critique_calibration.py` and
`tests/test_critic_rejection_regression.py` (incidental caveat kept,
missing requested date rejected, qualified partial kept, direct refusal
rejected).

### 2. The critic receives the complete structured report

`athena/agents/bb_io.py:177-181` renders the full `FinalReport` JSON (answer,
key points, citations, confidence, tools used, guardrail flags) instead of the
old answer/confidence/citation-list summary. The standard prompt consumes it at
`athena/agents/crew_roles.py:209`.

Covered by
`tests/test_critic_report_context.py::test_real_critic_prompt_contains_complete_final_report`.

### 3. Cited evidence is prioritised and audited

`athena/agents/bb_io.py:100-160` reserves one best evidence item for each report
citation within the unchanged eight-item budget, then fills remaining slots by
normal source priority. It emits a citation audit with four explicit statuses
(recorded and excerpted, recorded but outside the excerpt budget, recorded but
not citable, missing). The evidence/report context is at
`crew_roles.py:207-211`; the citation-audit guidance is at `:249-254`, telling
the critic that presence is not support and that an unshown excerpt is not a
missing reference.

The historical Day-1 rejection
`evaluation/quarantine/diagnostic_day1_failed/raw.jsonl`
(`gf-01__hierarchical__r0`) was a supported tungsten answer whose summary claims
the Wikipedia citation was not recorded. Reconstructing that run with the
current code shows all three citations as `recorded and excerpted`, including
`https://en.wikipedia.org/wiki/Tungsten`, and the deterministic calibration
leaves an approving critique approved. The critic's historical claim was false;
the current audit directly counteracts that class of context error.

Covered by
`tests/test_critic_report_context.py::test_critic_prioritizes_cited_source_and_reports_missing_ref`.

### 4. Standard and P2P critic prompts now share the qualification/severity policy

`athena/agents/crew_p2p.py:23-38` now has the qualified-partial exception,
missing-required-findings rejection, and high-severity limits that the blackboard
prompt has at `crew_roles.py:213-260`. Style/brevity are low/medium with
`approved=true` when the answer remains usable. Pattern/persona metadata is
explicitly execution context, not requested answer format.

Covered by
`tests/test_critic_rejection_regression.py::test_main_critic_prompt_preserves_qualified_partial_exception`
and `::test_p2p_critic_prompt_has_matching_criteria`.

### 5. Explicit N findings reaches both writers

The standard writer derives the count from task text only
(`athena/agents/crew_writer.py:217-239`). The P2P writer replaces the hard-coded
`3-5 key points`/`1-3 citations` text with the requested count
(`athena/agents/crew_p2p.py:56-64`). The finding requirement still forbids
inventing a source or finding to fill a count.

Covered by `tests/test_requirements.py` and
`tests/test_critic_report_context.py::test_writer_prompt_matches_requested_finding_count`
and
`tests/test_critic_rejection_regression.py::test_p2p_writer_count_does_not_conflict_with_requested_findings`.

### 6. Deterministic overclaim rejection is present on the standard path

`critique_rules.py:104-158` rejects approved prose that states a planned
capability as existing or uses a superlative none of its supporting evidence
uses. A direct probe (`calibrate` with the same report and evidence) returned
`approved=False`, `severity=high`, and
`claim exceeds the recorded evidence: ... (planned capability stated as existing)`.
This is stricter than before, not a loosening.

### 7. No rejection override and no threshold reduction

- `LOW_CONFIDENCE` is still `0.5` (`critique_rules.py:34`).
- Critic confidence ceilings are still `0.5` (high) and `0.6` (medium)
  (`athena/agents/confidence.py:28-29`, `:95-103`).
- `athena/agents/sufficiency.py` is untouched in this dirty tree; its
  `CEILING_*` values are unchanged.
- `calibrate` never sets `approved=True`; it only preserves a critique or
  changes it to `approved=False`.
- No production code sets `approved=True` (grep); the outcome readers are
  `athena/summary.py:321-335` and `athena/api/app.py:198-208`.
  `confidence.py` only lowers confidence. `summary.py:208-235` records a
  rejection as `rejected`.

The prompt wording change from an unconditional "confidence below 0.5 MUST be
false" to the assessment-conditioned qualified-partial exception is a scope
clarification, not a threshold reduction: the numeric threshold and the
deterministic check are unchanged, and the exception requires both a qualified
report and an assessment that says a qualified partial answer is possible.

## Historical rejection record (root-cause only)

- `evaluation/results/20261106T194245Z`: 4 rejected runs, all with
  `evidence_count=0`, `confidence=0.0`, and the same gap answer. These are
  correct rejections of an evidence-free answer, not over-rejection of a
  supported report.
- `evaluation/quarantine/diagnostic_day1_failed`: 10 missing critiques and 1
  rejection. The rejection is the tungsten case above; the audit now reports its
  citations as recorded. The batch remains quarantined and contributes no
  benchmark run.
- `20261107T133000Z`: 1 approved, 35 missing critiques. `20261107T150000Z`: 33
  missing critiques. `diagnostic_e2e_20261108`: 1 approved. None of these is a
  rejection-rate sample; most records never obtained a critic verdict.

No fresh representative live sample exists, and none was authorized.

## Remaining actionable gaps

### G1. P2P omits the deterministic overclaim calibration

`athena/patterns/p2p.py:285-295` calls
`calibrate(critique, report, assessment)` without `evidence_texts`; the standard
path supplies them at `athena/agents/crew_roles.py:276-281`. A controlled probe
with the same planned-capability overclaim returned:

- standard: `approved=False`, `severity=high`, issue
  `claim exceeds the recorded evidence: ... (planned capability stated as existing)`
- P2P-style: `approved=True`, `severity=low`, no issues

Reproducer: call `calibrate(Critique(approved=True), report, None,
evidence_texts=[...])` versus without `evidence_texts`, where the report says
`The Draco thruster is used on every flight.` and the evidence says
`The Draco thruster is planned for the capsule.` Fix: pass
`evidence_texts=[item.content for item in context.evidence]` through
`P2PAgent._audit_critique` and add a fake-only regression.

### G2. P2P finding-count extraction scans evidence, not just the question

`athena/agents/crew_p2p.py:47`, `:57-58`, and `:72` call
`finding_requirement`/`required_findings` on `instruction + context`, and the
context contains evidence. Reproducer:
`required_findings("Summarize the evidence.\n\nThe paper lists three key
changes.") == 3`. A fake-call probe of `p2p_write_report("r", "Summarize the
evidence.", context="...three key changes...")` produced
`The question requires exactly 3 findings` and `exactly 3 key points`, even
though the question asked for no count. The standard writer uses task text only
(`crew_writer.py:217,239`; `crew_roles.py:211`). Fix: detect the count from the
question/task portion only (or pass the question separately).

### G3. P2P critic evidence is not citation-aware and has no audit

`athena/patterns/context.py:59-85` sorts evidence by source type and takes the
first eight; it does not reserve the report's cited sources and emits no
citation audit. Probe with eight fetched pages plus one cited RAG item: the
report citation was present in the critic context, the cited RAG excerpt was
absent, and there were zero audit lines. This is the same context defect the
standard path fixed at `bb_io.py:100-160` and can cause a P2P critic to call a
recorded citation unsupported. Fix: apply citation-aware selection and an audit
in the P2P context builder (or pass a compact audit in the critic prompt).

### G4. P2P recovery ignores a high-severity critic rejection

`athena/patterns/p2p.py:129-133` starts the single recovery pass only when the
deterministic assessment says coverage is incomplete. A high-severity critic
rejection on an answerable assessment does not trigger the writer+critic prose
revision that sequential/parallel use at
`athena/patterns/recovery.py:348-386` and hierarchical uses at
`athena/patterns/hierarchical.py:163-169`. The report then ships rejected with no
revision attempt. This is a recovery asymmetry, not a weakened rejection rule;
add a bounded high-severity revision or document the intended difference.

### G5. Snippet-only partial answers are written but cannot be approved

`athena/agents/sufficiency.py:764` returns `partial_snippet_evidence=True` for
relevant snippets, but snippets do not set `background`, so `qualified_possible`
is `False` at `:501`; the writer caps confidence to the `0.3` ceiling at
`crew_writer.py:355`, and `calibrate` then rejects at medium because
`0.3 < 0.5` and the qualified-partial exception is unavailable. Probe result:
`search_only=True`, `qualified_possible=False`, `ceiling=0.3`,
`partial_snippet_evidence=True`, `calibrated approved=False, severity=medium`,
issue `report does not answer the core question (confidence 0.30 is below 0.5)`.
Decide the policy explicitly: if snippet-only qualified partials should ever be
approvable, define that in the assessment/ceiling; if not, document that they
are intentionally never approved. Do not lower `LOW_CONFIDENCE` to address it.

### Other low-risk observations

- `athena/patterns/context.py:129-145` keeps the report and analysis but trims
  evidence to fit the 9k budget; a maximal report plus status can omit evidence
  entirely. No focused test covers that boundary.
- The citation audit in `bb_io.py:136-160` is built from report-controlled
  `source_ref` values and is not inside `wrap_untrusted`; consider wrapping it
  or stripping newlines. This is prompt-injection hardening, not a rejection
  defect.
- The standard overclaim check uses every evidence text
  (`crew_roles.py:278-280`), including failed/non-citable `other` items, while
  the evidence excerpt is filtered to citable types. Low risk; align if the
  check ever proves noisy.

## Test execution record

### Environment and guard

Every test process ran with:

- `LITELLM_LOCAL_MODEL_COST_MAP=True`
- `CREWAI_TELEMETRY_DISABLED=true`
- `OTEL_SDK_DISABLED=true`
- `LLM_MODEL=openai/fake-model`, `LLM_API_KEY=inert-offline-test-key`,
  `LLM_API_BASE=http://127.0.0.1:9`
- inert `OPENAI/GROQ/DEEPSEEK/GEMINI/ANTHROPIC/OPENROUTER/MISTRAL/TOGETHERAI`
  keys and `*_API_BASE`/`OPENAI_BASE_URL` values at `http://127.0.0.1:9`
- `SEARCH_PROVIDER=searxng`, `SEARXNG_INSTANCE=http://127.0.0.1:9`,
  `SEARCH_FALLBACK_ENDPOINT=http://127.0.0.1:9`
- `ATHENA_REAL_STACK=0`, `PYTHONDONTWRITEBYTECODE=1`

A startup `sitecustomize.py` (session scratchpad, on `PYTHONPATH`) installed a
hard deny guard before pytest/framework imports: `socket.socket.connect`,
`socket.socket.connect_ex`, and `socket.create_connection` raise
`RuntimeError("NETWORK DISABLED by offline test guard")`. The only allowance is
the Windows stdlib `_fallback_socketpair` internal loopback connect, made
synchronously under a thread-local flag so `asyncio.run` can create its
self-pipe; a probe confirmed ordinary connects are denied and `asyncio.run`
works. No test failure was addressed by relaxing the guard.

All selected test bodies were reviewed first. `tests/test_searxng.py` was not
collected.

### Command 1

```
.venv\Scripts\python.exe -m pytest tests/test_critique_calibration.py tests/test_critic_rejection_regression.py tests/test_critic_report_context.py tests/test_requirements.py tests/test_p2p_critic_context.py tests/test_contract_real_functions.py -p no:cacheprovider -q -ra
```

Result: **36 passed, 1 skipped, 1 warning in 7.32s**.

- Skipped: `tests/test_p2p_critic_context.py:137`
  `test_the_critic_gets_the_report_through_the_real_supervisor` — requires a
  local A2A server and `ATHENA_REAL_STACK=1`; no server was started.
- Warning: ADK experimental `PLUGGABLE_AUTH` feature warning from the installed
  package.

### Command 2

```
.venv\Scripts\python.exe -m pytest tests/test_research_routing.py tests/test_reliability_claims.py -p no:cacheprovider -q -ra
```

Result: **46 passed, 1 warning in 4.73s** (same ADK warning).

No failures needed retention. Total across the two focused commands:
**82 passed, 1 skipped**.

### Direct deterministic probes (no pytest, no model call)

- Historical tungsten audit: all three citations reported `recorded and
  excerpted`, including the Wikipedia reference the old critique called missing.
- Historical tungsten calibration: an approving critique remained approved
  (`approved=True`, `severity=low`, no issues) when reconstructed with the
  current assessment and evidence.
- Standard versus P2P overclaim calibration: standard rejected the planned-
  capability overclaim at high severity; the P2P-style call without
  `evidence_texts` approved it.
- P2P count scope: `required_findings` returned `3` from evidence prose; the
  real `p2p_write_report` prompt then required exactly three key points.
- P2P cited evidence: a cited RAG item was absent from the eight-item critic
  context while its report citation was present, with no audit line.
- Snippet-only partial: `partial_snippet_evidence=True`,
  `qualified_possible=False`, ceiling `0.3`, deterministic medium rejection.

These probes imported only pure role/assessment modules, made no model or
provider call, and are evidence of current behavior, not substitutes for the
focused pytest results above.

## Adjacent observation (out of scope)

While this review was running, `evaluation/runner.py:592-627` changed to the
authorized per-agent token aggregation: it now keeps `model_call` usage per
agent, falls back to `a2a_call` usage only when no measured model usage exists,
preserves the separate model/tool/recovery/error counters, and counts
`model_calls` only from `model_call`. `tests/test_evaluation_event_totals.py`
is the accompanying new test file. I did not run that file or modify that code;
the parent owns that slice.

## Bottom line

The standard-path false-rejection fixes identified in the handoff are
present and covered in this dirty tree: assessment-gated gap wording, the full
report, and citation-aware evidence with an audit. Standard/P2P prompt criteria
and explicit-N writer prompts have also been updated, subject to the P2P count
scope gap above. No rejection override or threshold
reduction was introduced. The P2P path still has three concrete parity gaps
(deterministic overclaim calibration, count scope, cited-evidence audit) and a
recovery asymmetry; those are the next candidates. The historical rejection
concern is not empirically resolved by these fixes, and this review did not
obtain a representative live sample.


## Implemented P2P follow-up (2026-11-09)

The user authorized the P2P fixes after the read-only review. Implementation used a Local ACP DeepSeek v4.1 Flash worker; the parent reviewed the changes and added the empty-evidence citation-audit regression.

- G1: `_audit_critique` now calibrates against the inline report and citable inline evidence. Both the persisted critique and carried context use the calibrated verdict, so a raw model approval cannot hide an overclaim.
- G2: P2P context carries an explicit question boundary. Finding counts come only from that question; legacy direct role calls use their instruction. Evidence, analysis and report text are excluded from count extraction.
- G3: the standard and P2P paths share `bb_io.select_critic_evidence`. Cited sources have priority within eight items, and audit statuses reflect the excerpts actually retained after character trimming. Source references are not echoed into audit instructions. Empty recorded evidence still produces a missing-source audit for report citations. The P2P context cap remains 9,000 characters.
- G4: a high-severity rejected critique with sufficient evidence triggers one writer-plus-critic prose revision with calibrated critique feedback, without another research call. Persistent rejection remains rejection; low/medium or approved verdicts do not trigger this revision. Research recovery stays separately bounded, and both counters are emitted.

Changed implementation: `athena/patterns/p2p.py`, `athena/patterns/context.py`, `athena/agents/crew_p2p.py`, `athena/agents/bb_io.py`. Regression coverage: `tests/test_p2p_critic_parity.py` plus the adjacent existing suites.

Parent verification: **143 passed, 1 skipped** in the focused P2P, critic, context, requirement, claim/routing, A2A and writer suites. The existing real-stack test was skipped with `ATHENA_REAL_STACK=0`. The session-only offline launcher denied outbound socket connections before project imports (apart from the Windows stdlib internal socketpair), disabled telemetry and set inert keys and localhost:9 endpoints. No server, live provider, evaluation batch, deployment or commit was performed.

No rejection override or confidence-threshold change was made. G5 remains the conservative snippet-only policy: a qualified snippet draft is not guaranteed an approving critique. Live acceptance/rejection rates remain unmeasured. Historical data and scripts/dev_up.py are untouched.
