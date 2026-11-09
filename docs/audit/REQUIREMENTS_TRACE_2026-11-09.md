# Requirements and implementation audit — 2026-11-09

## Checkpoint and method

The ADK/CrewAI implementation and evaluation harness are substantial, but the
repository does not yet support a defensible completed-benchmark claim. Reporting
integrity, security audit coverage, and documentation accuracy need attention
before another experiment or a teacher-facing performance report.

This checkpoint follows `docs/SESSION_HANDOFF_2026-11-09.md`: inspect requirements,
map source and tests, then propose a separate implementation stage. Three workers
used Local ACP, model `deepseek/deepseek-v4.1-flash`, effort `max`. The root reviewed
the consequential findings and reconciled disagreements below.

- Snapshot: branch `main`, HEAD `cad39167994dccd0728ae3a4d03ba0b3b8b1dc80`, with
  existing uncommitted implementation, tests, configuration, and documentation.
- Application source, configuration, datasets, historical records, and test bodies
  were inspected. Tests, application servers, browsers, benchmarks, and application
  model/provider calls were not run during this checkpoint. The requested ACP
  coding workers were used; Athena's inference configuration was not changed.
- This document is a new audit artifact. Existing files and historical data were
  not edited, regenerated, moved, committed, or deployed.
- References below use paths and line numbers from the dirty working tree.
  They will drift when later work changes those files.
- **Source confirmed** means an implementation or defect is visible in inspected
  code. **Test present** means an assertion exists, not that it passed now.
  **Historical** means a prior document or handoff reports an observation.
  **Unverified** means this checkpoint did not establish runtime behavior.

## Requirement authority

`docs/GUIDELINES.md:8` contains eleven numbered project rules. Its status column
and Stage-0 introduction are outdated; the rules themselves remain a useful
requirement source. The current session handoff controls phase boundaries,
preservation, benchmark hold, framework choice, unchanged thresholds/limits, and
UI acceptance status. The older attached project handoff supplies context and
the teacher report/technical appendix requirements where the newer handoff does
not supersede it.

README descriptions and old verification documents are claims to check, not
proof of implementation or current test results. No grading rubric was found
that requires persona metadata to be a runtime treatment or specifies a
cross-run evaluation heatmap. Those are clarification points, not established
missing requirements.

## Code-and-test evidence map

### R1 — Blackboard sharing

**Source confirmed:** `athena/blackboard/sqlite_store.py:39` implements SQLite
storage, WAL/concurrency handling and validated writes;
`athena/blackboard/sqlite_schema.py:20` protects history against UPDATE/DELETE.
`athena/blackboard/schemas.py:216` registers the sections. The standard patterns
share run-scoped state; P2P carries stage content in messages and writes audit
records, while still reading configuration/task metadata from the Blackboard.

**Test present:** `tests/test_blackboard.py:118` covers history behavior;
`tests/test_patterns.py:146` covers concurrent evidence append.

**Gap:** `docs/BLACKBOARD.md:20` omits `analysis` and describes a
`guardrail_flags` section writer that the worker did not find in production.
The toolset-health mirror also targets an unregistered section; event logging
and the in-memory fallback are distinct from durable Blackboard persistence
(`athena/agents/toolset_health.py:136`, `athena/blackboard/validation.py:23`).

### R2 — At least two tools over MCP

**Source confirmed:** five tool IDs over three local stdio servers:
`rag_search`, `web_search`, `fetch_page`, `calculate`, and `forecast`
(`athena/tools/registry.py:29`). ADK builds filtered toolsets at
`athena/agents/researcher.py:132`; before/after callbacks enforce selection and
record screened evidence (`:189`, `athena/agents/evidence_recorder.py:171`).

**Test present:** `tests/test_research_tools.py:235` checks schema filtering;
`:249` checks tool-selection enforcement. `tests/test_tools_mcp.py:30` marks
stdio integration tests as e2e; in-process adapter tests do not prove that wire
path ran successfully now.

**Gaps:** MCP environment inheritance and P2P cleanup are listed below.
`docs/AGENTS.md:68` overstates failed-tool exclusion: failed fetches can be
recorded as non-citable `other` provenance, unlike successful evidence
(`athena/tools/adapters.py:154`, `tests/test_tools_adapters.py:132`).

### R3 — Pydantic structured final output

**Source confirmed:** `FinalReport` is defined at
`athena/blackboard/schemas.py:158`; writer generation and persistence use it
(`athena/agents/crew_writer.py:244`). Sanitization and secret redaction run
before report persistence (`athena/agents/report_sanitize.py:48`).

**Test present:** `tests/test_agents_writer.py`,
`tests/test_report_secret_redaction.py`, and
`tests/test_critic_report_context.py:41` exercise writer/report boundaries and
the critic's complete structured context.

**Limit:** schema validity and resolving references do not establish that a
source supports a claim. Live critic/provider behavior was not retested.

### R4 — Configurable orchestration patterns

**Source confirmed:** the registry and CLI expose sequential, parallel,
hierarchical, and P2P (`athena/patterns/__init__.py`, `athena/run.py:532`).
Sequential includes a final recovery-review stage
(`athena/patterns/sequential.py:20`); parallel has concurrent assigned workers
and recovery review (`athena/patterns/parallel.py:63`). Hierarchical dispatches
plan assignments and has bounded revision/research recovery
(`athena/patterns/hierarchical.py:87`). P2P carries inline context and can run
one additional research/role pass (`athena/patterns/p2p.py:122`).

**Test present:** `tests/test_patterns.py:107` pins sequential stage order;
`:128` covers worker overlap; `:221` covers hierarchical recovery; `:368`
covers the revision limit. P2P has separate tests in `tests/test_p2p.py`.

**Gap:** `README.md:386`, `docs/PATTERNS.md:37`, and `docs/FLOW.md:144`
understate recovery outside hierarchical mode. Comparisons must describe the
actual work and recovery budgets, not merely the initial five-stage diagram.

### R5 — Callback monitoring and logging

**Source confirmed:** researcher callbacks enforce tool policy and record tool
results (`athena/agents/researcher.py:265`, `:309`, `:387`), with observability
recorder/hooks and event persistence. CrewAI role execution emits model events
(`athena/agents/crew_roles.py:123`).

**Test present:** `tests/test_observability_hooks.py`,
`tests/test_evidence_authorship.py`, and `tests/test_observability_writer_a2a.py`.

**Gap:** accepted-message A2A warning flags are discarded on the server path;
see finding A3. The outdated checklist still describes monitoring as absent.

### R6 — A2A between ADK and CrewAI

**Source confirmed:** the client uses the official SDK's card resolver and
client factory (`athena/agents/a2a_client.py:20`, `:94`). The server uses the
SDK executor/request handler, an agent card, JSON-RPC and HTTP+JSON routes
(`athena/agents/crew_server.py:282`). Roles dispatch on Athena's JSON body.
Blackboard messages carry run ID/role/instruction; P2P messages add context and
return content. Optional HMAC signing is application-specific.

**Test present:** `tests/test_agents_a2a.py:64` invokes the real dispatch helper
with fake handlers. `tests/test_p2p_critic_context.py:137` contains a gated real
client/server test. Direct dispatch tests do not establish wire interoperability.

**Limits:** no fresh handshake with an independent client; in-memory task store;
no cancellation implementation (`crew_server.py:301`); streaming/push disabled
(`:315`); no advertised card security schemes. The card advertises the fixed
localhost:9001 base URL (`:61`, `:305`), which is a static risk for the dynamic-port
integration test. This checkpoint did not reproduce that failure.

### R7 — Only ADK and CrewAI as agent frameworks

**Source confirmed:** inspected orchestration uses ADK agents and CrewAI crews,
with A2A/MCP as protocols. CrewAI role crews are sequential and disable
delegation (`athena/agents/crew_roles.py:32`). Pins are in `requirements.txt`.
No third orchestration framework was identified on the traced execution paths.

**Limit:** no complete dependency/transitive-package audit was performed.

### R8 — Guardrails

**Source confirmed:** input screening is called from research and chat entry
points (`athena/run.py:95`, `athena/chat/service.py:112`). Inter-agent validation
is called at `athena/agents/crew_server.py:195`; evidence/tool screening and
report sanitization have production callers. Secret-redaction and retry fixes
from the handoff remain present.

**Test present:** `tests/test_run_input_guardrails.py:89`,
`tests/test_security_integration.py:225`,
`tests/test_observability_writer_a2a.py:221`, and
`tests/test_report_secret_redaction.py`.

**Limits:** lexical screening is not semantic protection. Analyst/critic prose
does not have the same final-output screening path as the persisted report.
A2A warning audit completeness and MCP environment inheritance need repair;
these are not evidence of an exploited system.

### R9 — Observability and tracing

**Source confirmed:** persisted event records, per-agent metrics, telemetry API,
and per-run heatmap PNG/CSV exist (`athena/observability/metrics.py:112`,
`athena/observability/heatmap.py:47`, `athena/api/app.py:349`).

**Test present:** `tests/test_observability_report.py:68` and
`tests/test_telemetry_routes.py:270` cover heatmap artifacts/routes.

**Gap:** the evaluation runner aggregates tokens differently from the
observability metrics implementation; see A1. Existing local tracing should
not be described as an OpenTelemetry deployment.

### R10 — Evaluation with many runs and boxplots

**Source confirmed:** schema/dataset, plan validation, executors, locking,
resumption, deterministic scoring, analysis, CSV exports, reports and charts
exist (`evaluation/schema.py:608`, `evaluation/runner.py:1366`,
`evaluation/scoring.py`, `evaluation/analyze.py:613`). The evaluation API serves
summaries/charts (`athena/api/eval_routes.py:274`).

**Fresh read-only JSON counts:** the dataset has 48 items in 12 categories.
The 30-entry plan has 30 unique keys and 24 prompts; the 36-entry plan has 36
unique keys and 24 prompts. Their configured attempt caps are 36 and 40,
respectively. The general config has 72 entries, 72 unique keys and 30 distinct
prompts. Therefore 40 is not evidence of a completed or configured 40-entry
experiment. The separate NASA 40-question CSV is another artifact.

**Test present:** `tests/test_evaluation_plan_hierarchical.py`,
`tests/test_evaluation_runner.py:197`, `:222`,
`tests/test_evaluation_output_lock.py:82`,
`tests/test_evaluation_quarantine.py:119`, and
`tests/test_eval_quarantine_routes.py:51`.

**Incomplete:** harness implementation does not fulfill empirical validation.
No valid completed 30-run result exists. The contaminated Day 1 records are
quarantined and contribute zero of the planned runs. Historical failed,
aborted, diagnostic and implementation-invalid batches cannot establish a
pattern ranking. Boxplots exist; the inspected heatmap is per-run observability,
not a cross-run evaluation heatmap.

### R11 — Cloud deployment

**Artifacts present, deployment incomplete:** `Dockerfile`, `fly.toml`,
`render.yaml`, and `docs/DEPLOYMENT.md` exist. The latter states not deployed.
Deployment remains deferred under the handoff; its existence as a project rule
does not authorize deployment now.

## Findings that affect the next stage

### A1 — Evaluation double-counts CrewAI token usage

**Source confirmed, high priority.** `evaluation/runner.py:592` adds tokens from
both `model_call` and `a2a_call` events. The CrewAI server records its usage as
model events (`athena/agents/crew_roles.py:123`), and the ADK A2A stage records
the returned usage again (`athena/agents/a2a_stage.py:91`). The monitoring
metrics explicitly prefer server usage and use acknowledgements as fallback
(`athena/observability/metrics.py:112`).

This inflates evaluation token totals when both event sources are present and
can inflate estimates derived from those totals. No billing reconciliation was
performed. Existing executor tests stub `event_totals`
(`tests/test_evaluation_runner.py:429`), so they do not establish correctness
of this aggregation boundary.

### A2 — Failed security attempts are described as measured resistance

**Source confirmed, high priority.** `evaluation/scoring.py:809` sets
`injection_obeyed=False` when no marker appears, including an empty answer.
`evaluation/analyze.py:1142` reports the complement as "resisted". Similarly,
no substantive answer produces `abusive_input_handled=False`, whose complement
is called "refused or deflected". A provider failure is not evidence that an
agent resisted injection or chose to refuse.

Keep attempts/failures visible, but report security behavior only for evaluable
outputs, with explicit not-evaluable counts. Do not discard real behavioral
failures or change acceptance thresholds. `unsupported_claim_rate` also returns
zero for no claims (`evaluation/scoring.py:363`); that value must not be
presented as measured factual support for an unanswered attempt.

### A3 — Accepted A2A warning flags are not persisted

**Source confirmed, high priority.** `crew_server.py:195` calls
`validate_message(..., check_signature=False, emit=False)`. `_emit_verdict` is
called on rejection branches, but not after an accepted verdict. Injection
warnings produced at `athena/guardrails/interagent.py:309` are therefore lost
on this path. With a shared token configured and required-signature mode off,
`peer_auth_error` accepts an unsigned message (`crew_server.py:73`), while the
validator's unsigned-warning branch is skipped (`interagent.py:282`).

Signature enforcement itself is not absent: mismatches and required missing
signatures are rejected. The defect is silent allowed traffic/warnings versus
the documented audit promise. Any repair must preserve safe run attribution;
do not write events to an unverified message-supplied run ID.

### A4 — MCP subprocesses inherit the A2A shared token if configured

**Source confirmed, high priority with a local trust boundary.**
`athena/agents/researcher.py:112` allows every `ATHENA_*` environment variable,
which includes `ATHENA_A2A_SHARED_TOKEN`. The helper's stated intent is to pass
settings without model secrets. `tests/test_research_tools.py:771` checks an
LLM-key exclusion but not this token.

This grants a tool subprocess credentials intended for A2A peers. No actual
token value was inspected and no external disclosure was demonstrated. A
focused secret-exclusion test and a reviewed settings allowlist/exclusion are
appropriate; changing authentication policy is a separate decision.

### A5 — Resumed batches reset invocation budgets

**Source confirmed; intended cumulative semantics need definition.**
`BudgetTracker` initializes runs/spend/time to zero on construction
(`evaluation/runner.py:1200`). Resume loads prior records for selection and
attempt numbering (`:1349`), without restoring those budget totals. CLI caps
replace the configured invocation caps (`:1302`). Thus the configured attempt,
USD and wall-time limits are not cumulative lifetime limits for a results
directory. A single-invocation cap test (`tests/test_evaluation_runner.py:222`)
does not prove a multi-day batch ceiling.

Do not claim that a future batch will necessarily make 42 attempts: actual
attempts also depend on selections, retry eligibility and operator commands.
Define separate invocation and persisted batch budgets before changing their
meaning. Unpriced costs remain null and cannot bound provider spending.

### A6 — Repeated run keys can acquire another attempt's score

**Source confirmed.** `Analysis.by_key` retains one score per run key;
`records_with_scores` attaches that score to every matching record
(`evaluation/analyze.py:159`). The key can repeat through legitimate retries
or across pooled historical batches. Record-level comparisons/appendices can
then associate one attempt's metrics with another attempt's rubric result.
Use attempt/batch identity or preserved record-score pairing; add coverage for
different answers under the same run key.

### A7 — Historical report eligibility and denominators need explicit policy

`evaluation/report.py:87` discovers all unmarked result directories, including
the diagnostic smoke batch and older failed/aborted batches. Its pooled report
already warns that it is not one experiment, but that warning does not make
the pooled quality figure a benchmark measurement. `evaluation/REPORT.md:3`
predates the diagnostic and lists 75 records in three directories; the worker
found 76 records in the current four-directory result tree. It was not
regenerated here.

`is_valid_completed` requires a completed state, a nonempty answer and a
non-infrastructure classification (`evaluation/outcomes.py:280`). It does not
exclude the historical B5 batch that `evaluation/BUGS_FOUND.md:22` says was
invalidated by toolset failure. That batch therefore contributes to the old
pooled quality sample.

**Policy caution:** do not "fix" quality by excluding every low-evidence or
rejected completion. Honest abstentions and poor completed answers may be valid
experimental outcomes. Preserve genuine zero scores
(`tests/test_evaluation_metrics.py:163`). Use explicit batch eligibility and
report evaluability rather than post-hoc score improvement. Per-pattern critic
approval and attempt-level rubric statistics also use different denominators
from the operational critic/valid-completion block
(`evaluation/analyze.py:245`, `:258`, `:263`); label them precisely.

### A8 — Other accounting and configuration gaps

- Cost estimation uses aggregate input/output tokens and one selected model
  (`evaluation/runner.py:756`), while provenance permits different ADK/CrewAI
  models. Mixed-model pricing requires per-model usage. Separately recorded
  reasoning tokens must follow the provider's billing semantics; adding them
  blindly can itself double-count output usage.
- `evaluation/report.py:621` expects `pricing_summary`, while normal batch meta
  writes `pricing` and `pricing_status` (`evaluation/runner.py:1666`). The
  configured-model summary can consequently remain unknown.
- `matrix.tool_selection` and `matrix.llm_call_cap` are recorded in metadata
  (`runner.py:713`) but are not forwarded by the inspected executors
  (`:900`, `:979`). This is a latent contract gap when nondefault values are
  requested, not proof that the current default selection differs.
- The runner defaults its dataset argument independently of
  `config.dataset_path` (`runner.py:1824`, `:2039`), while metadata records the
  configured path and hashes the loaded path (`:1657`). Current plans coincide;
  a custom dataset could expose the mismatch.
- The evaluation API returns an interpretation caveat, but the inspected UI
  does not render that note (`athena/api/eval_routes.py:322`,
  `athena/api/static/app.js:3156`).

### A9 — P2P toolset cleanup has an ownership gap

**Source confirmed lifecycle risk, not a reproduced process leak.** P2P builds
its initial/recovery researchers as local variables
(`athena/patterns/p2p.py:105`, `:144`) and does not close their toolsets in a
local finally block. Normal runner cleanup traverses owned attributes of
`runner.agent` (`athena/run.py:221`, `athena/agents/lifecycle.py:102`), not those
locals. Existing cleanup tests cover supervisor, parallel and execute failure
paths (`tests/test_reliability_lifecycle.py:397`, `:409`, `:425`). Add P2P
normal/error/cancellation coverage before claiming lifecycle closure.

## Personas, datasets, UI and reporting readiness

**Personas:** the dataset has expert/novice/hurried/hostile/manipulative labels
(`evaluation/schema.py:50`). `Task.persona` exists
(`athena/blackboard/schemas.py:49`), but normal task construction does not set
it (`athena/run.py:100`); the evaluation executor forwards prompt/pattern,
not the separate persona field (`evaluation/runner.py:979`). The labels are
strata, not an independently injected runtime treatment. Some prompts themselves
express style or hostile behavior, so labels being unforwarded does not imply
all prompts are behaviorally identical. A controlled persona-variant experiment
has not been demonstrated or established as a requirement.

**Dataset provenance:** structural validation is implemented and workers
inspected counts/rubric consistency. Semantic answer-key verification,
authorship/date provenance, and the relationship between the 48-item evaluation
JSON and `data/eval/nasa_rag_40_questions.csv` are not adequately documented.
Seeds are trial labels, not provider-seeded reproduction
(`evaluation/runner.py:1647`).

**UI:** the current context rail/modal behavior exists in JS/CSS
(`athena/api/static/context.js:68`, `:100`, `:202`,
`athena/api/static/style.css:1345`). Static/logic tests cover parts of its
wiring. The handoff additionally records browser visual QA and three saved
screenshots; that historical evidence must not be erased by calling the UI
"string-tested only". No fresh browser/layout-shift measurement or user visual
acceptance occurred here. Visual acceptance remains outstanding.

**Teacher-facing report:** purpose, architecture, workflow, agent responsibilities,
planned design, metric definitions and limitations can be drafted from corrected
documentation now. A report may describe engineering validation and historical
diagnostics with their limits. Performance conclusions, comparative rankings,
provider spend and a completed benchmark results section are not ready.
The generated run-appendix table (`evaluation/report.py:678`) does not yet contain
the requested token/evidence/citation/critic detail; richer CSV/raw records exist
but must be mapped into the deliverable explicitly.

## Documentation corrections to scope separately

- `docs/GUIDELINES.md:4`: Stage-0 text and reserved-package/todo notes no longer
  describe implemented patterns, callbacks, guardrails, tracing or the harness.
  Distinguish implemented harness from completed empirical evaluation, and
  deployment files from a deployed service.
- `docs/AGENTS.md:3`: stale baseline introduction; also update P2P contract,
  recovery, evidence failure handling, authorship and conflicting token budgets.
- `docs/PATTERNS.md:37`, `docs/FLOW.md:144`, `README.md:386`: document bounded
  recovery accurately across all patterns.
- `docs/benchmark/PREFLIGHT_30.md:3`: historical "AUTHORISED — proceeding" needs
  a superseding HOLD notice pointing to the current handoff.
- `docs/audit/FINAL_VERIFICATION.md:3`: explicitly audits old commit `117ed96`.
  Keep it as history; do not cite its obsolete missing-caller conclusions as
  current facts. Some guardrail/retry callers have since been added.
- `evaluation/REPORT.md`: mark the saved aggregate as historical/superseded
  without regenerating it or silently changing raw evidence.
- `README.md:814`: "no cost accounting" should distinguish existing local
  estimates from unavailable provider billing verification and unpriced models.
- Clarify handoff references to "40-run benchmark" using the actual 36-plan
  40-attempt cap. Do not invent a new plan or alter 30/36-run plans.
- The older attachment's `docs/benchmark/DIAGNOSTIC.md` path is stale; the
  actual diagnostic note is under
  `evaluation/results/diagnostic_e2e_20261108/DIAGNOSTIC.md`.

## Validation limits and safe test planning

No test pass counts are newly established. The handoff's focused stage results
remain historical, overlapping counts, not a complete repository run. Counting
test function definitions is not equivalent to counting collected/parameterized
pytest cases and is not evidence they pass.

The default `not e2e` marker selection is not an offline guarantee:
`tests/test_searxng.py:424` probes localhost at import/collection time and the
unmarked live test at `:427` can call a running search service. Merely deselecting
the test by name may still import the file and execute the probe. For a strict
offline slice, select reviewed files that do not import that module, or ignore
the entire file before collection. Active profiles and telemetry/cost-map
loading also need guards. The worker's broad "everything except two tests"
suggestion was not accepted as a validated safe suite.

Any implementation checkpoint should review its focused test bodies, retain
fake generation below the real function under test, use inert credentials and
localhost:9 endpoints as appropriate, disable telemetry/remote cost-map access,
and prevent profile overrides from restoring live inference. Do not invoke the
benchmark, demo replay, or provider probes as a verification shortcut.

## Proposed implementation sequence — not started

1. **First narrow checkpoint: evaluation token aggregation only.** Repair
   `event_totals` to prefer authoritative per-agent model usage and use A2A
   usage only as fallback, preserving distinct model/tool/recovery/error counts.
   Reuse the existing monitoring policy where appropriate without changing
   rates, limits, provider configuration, raw historical data, or scoring.
   Add small fake-event tests for mixed ADK/CrewAI events, ack-only fallback,
   model-only usage, reasoning fields, and multiple CrewAI roles. Validate the
   real aggregation helper and the relevant executor/report boundary offline.
   Acceptance: mirrored CrewAI events contribute once and ADK usage remains
   included. No benchmark or report regeneration is part of this slice.
2. **Documentation truth repair:** add the HOLD/historical notices and reconcile
   the specified architecture/status claims. Preserve original experimental
   records. This can follow independently of a new experiment.
3. **Security boundary and audit slice:** exclude A2A credentials from MCP
   inheritance and preserve accepted A2A warnings with verified attribution.
   Exercise real dispatch/environment helpers with fakes. Do not silently
   change optional-signature policy.
4. **Evaluation eligibility/reporting slice:** make attempt identity and
   security evaluability explicit; define cumulative versus invocation budgets
   and batch eligibility. Preserve poor completed answers and genuine zeros;
   do not improve rates by filtering outcomes after seeing their scores.
5. **Lifecycle/interoperability slice:** P2P close-on-exit coverage and a real
   local A2A SDK handshake using fake role handlers and configurable card URL.
   Keep live model inference disabled.
6. **Later checkpoints:** teacher report/appendix structure, dataset provenance,
   any rubric-required persona experiment, and user visual acceptance. Fresh
   benchmark execution and deployment remain explicitly deferred.

The handoff requires phased user checkpoints and forbids inferring authorization
for the next phase. This audit creates a reviewable proposal; it does not start
any of the implementation slices above.


## Authorized correctness follow-up (2026-11-09)

The user subsequently authorized fixing the identified correctness issues, while keeping deployment and evaluation runs excluded. The original audit above remains a historical checkpoint.

- Evaluation token aggregation selects one source per agent, preserving model usage with mirrored A2A fallback instead of adding both. Mixed-model costs now use per-agent usage and reported/configured framework models. Missing rates or unattributed mixed-model totals remain unknown.
- Resumed batches restore prior attempts, known spend, unknown-cost counts, and execution time; prior per-run cost stops also survive restart. Older records supply latency lower bounds when elapsed-time metadata is absent.
- Analysis pairs scores by attempt identity, rejects ambiguous legacy joins and duplicate identities, and preserves pairing across merged batches. No-answer security outcomes and unsupported-claim rates are unevaluable, with explicit denominators. Reports read canonical pricing metadata and honor the ledger when unknown costs make USD caps unenforceable.
- Accepted A2A messages now emit their computed warning flags through the same trusted-attribution, redacting audit helper used for rejections.

Parent verification at this checkpoint: 199 focused evaluation tests passed; 19 A2A/writer tests passed separately. Both commands used the session-only offline launcher with outbound socket connections denied, telemetry disabled, inert provider keys, and localhost:9 endpoints. These are regression tests with fakes, not evaluation batches or live acceptance measurements. P2P critic gaps G1-G4 are now fixed; the subsequent focused check passed 143 tests with one real-stack skip. Current details are recorded in CRITIC_STATUS_2026-11-09.md. Historical results and scripts/dev_up.py remain untouched.
