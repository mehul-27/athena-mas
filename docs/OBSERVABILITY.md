# Observability: callbacks, event log, metrics, heatmap

Course rule 5 (callback-based monitoring and logging). Everything here is
**passive**: it observes the pipeline, it never changes what the agents do.
`ATHENA_MONITORING=0` turns every callback into a no-op and the run behaves
identically.

## Where the data lives

| Store | Path | Owner |
|---|---|---|
| Event log | `EVENTS_DB`, default `data/events.db` | both processes, append-only |
| Blackboard | `BLACKBOARD_DB`, default `data/blackboard.db` | both processes |
| Rate limiter | `RATELIMIT_DB`, default `.run/ratelimit.db` | both processes |

The event log is a separate SQLite file, so monitoring can be dropped or
archived without touching the blackboard. Like the blackboard history it has
`BEFORE UPDATE`/`BEFORE DELETE` triggers: an event, once written, cannot change.

## Event kinds and what emits them

| kind | emitter | fields worth reading |
|---|---|---|
| `stage_start` | ADK plugin `before_agent_callback`; CrewAI `run_role_request` | agent |
| `stage_end` | ADK plugin `after_agent_callback`; CrewAI `run_role_request` | duration_ms, tokens_in/out (ADK stage total) |
| `model_call` | ADK plugin `after_model_callback`; CrewAI `_crew_call` | tokens_in, tokens_out, duration_ms |
| `tool_call` | ADK plugin `after_tool_callback` (one per call) | payload_summary = tool + args, ok, duration_ms |
| `a2a_call` | `A2AStageAgent` / `P2PAgent` after the role returns | bytes in/out in `extra`, tokens from the ack |
| `blackboard_write` | `Blackboard` write listener | section, version, author is the agent |
| `limiter_wait` | `TokenLimiter.acquire` when a wait exceeds 50 ms | duration_ms, tokens requested |
| `error` | plugin `on_model_error_callback`; `Recorder.span`; CrewAI role failure | error_type, error_message (redacted) |
| `guardrail` | the guardrail module that made the decision (see below) | stage, pipeline_stage, rule, action, detail (redacted) |
| `retry` | a call site that re-ran work (see below) | retry_type, attempt, of, reason, token_budget, inferred |
| `handoff` | `MonitoringPlugin._emit_handoff`; `hooks.emit_handoff` | via, role, from, to |

The final event of a run is a `stage_end` with agent `supervisor` whose `extra`
is the whole `RunSummary` (pattern, wall_s, stage_ms, adk_tokens, a2a bytes,
evidence count, tool calls, revisions, confidence, approved). Step 9 reads the
summary from there.

## Which hook mechanism, and why

- **ADK: a plugin** (`MonitoringPlugin`), registered on the runner with
  `plugins=[...]`. It applies to every agent the runner executes — including the
  custom supervisor and the A2A stage agents, which are plain `BaseAgent`s and
  would each need their own callbacks otherwise. ADK also runs plugins before
  agent callbacks, so nothing is missed.
- **CrewAI: the single entry point** `_crew_call` plus `run_role_request` in the
  server. Every role goes through them, so one span per role covers all three.
  Token usage comes from `CrewOutput.token_usage` (`prompt_tokens`,
  `completion_tokens`) and is returned in the A2A ack, because that process's
  numbers are invisible to the runner otherwise.

## Redaction and truncation

Before an event is stored, every text field passes through `redact()`: exact
values of environment variables whose names contain KEY/TOKEN/SECRET/PASSWORD/
CREDENTIAL are replaced with `[REDACTED]` (compared in memory, never printed),
as are token-shaped strings (`sk-…`, `gsk_…`, `Bearer …`, 40+ char blobs).
`error_message` and `payload_summary` are truncated to 300 characters and
`extra` is cut down to small, JSON-safe values.

## Reading the heatmap

```
python -m athena.observability.report --run <run_id> --out .run/report
python -m athena.observability.report --last 2 --out .run/report2
```

The report prints one event table per run and the per-agent metrics
(`stage_ms`, `model_calls`, `tokens_in`, `tokens_out`, `tool_calls`, `errors`,
`limiter_wait_ms`, `a2a_bytes`, `evidence_items`), then writes:

- `heatmap.png` — rows are agents, columns are the metrics above. **Each column
  is normalised 0-1 on its own**, so a dark cell means "large for this agent
  compared with the others in this column", not "large in absolute terms". The
  raw value is printed in every cell; the colour is only a guide.
- `heatmap.csv` — the same numbers, for anything that needs to be plotted
  elsewhere.

A single-run heatmap is a snapshot, not a comparison. Use `--last N` to
aggregate.

## Not covered yet

- **Distributed tracing** (OpenTelemetry spans across the A2A hop) is Step 8.
- No live tail/TUI, no retention policy, no alerting: the event log grows.
- CrewAI's token numbers are per crew call, not per provider request: a crew
  that retries internally still reports one number.

## Reasoning models

A reasoning model spends output tokens on hidden reasoning before it writes
anything visible. Two consequences are built in:

- **Budgets are generous and per role** (planner 1500, researcher 2000, analyst
  1500, writer 3000, critic 1500; `LLM_MAX_TOKENS_<ROLE>` still wins).
- **`LLM_REASONING_EFFORT`** (`low|medium|high`) is forwarded to both frameworks
  when set, and not sent at all when empty.

When a call comes back with no visible text and `finish_reason` is `length`, the
model layer retries **once** with the output budget doubled (capped at 4000) and
records an `error` event with `error_type: "empty_reply"` on every occurrence.
CrewAI exposes no `finish_reason`, so there an empty result alone triggers the
retry.

`model_call` events carry `extra.reasoning_tokens` (ADK:
`usage.thoughts_token_count`, which is where ADK puts litellm's
`completion_tokens_details.reasoning_tokens`; CrewAI: `UsageMetrics.reasoning_tokens`,
also echoed in the A2A ack). `tokens_out` stays the **total** completion count,
reasoning included, so `tokens_out - reasoning_tokens` is the visible output.
The metrics table, the heatmap and its CSV all carry the column.

## CrewAI usage capture: why the scope was empty

Step 6f captured usage in `scripts/preflight.py` but recorded `calls == 0` in
the real server. The causes were checked one by one against the installed
sources:

| # | Candidate | Verdict |
|---|---|---|
| a | logger never installed at server startup | **No.** `python -m athena.agents.crew_server` (`crew_server.py:193`) calls `create_app()`, which runs `configure_process()` and `install_usage_logger()` in the serving process. uvicorn runs one process, no reload. |
| b | litellm dispatches callbacks on another thread/task | **No for these calls.** `success_handler` (`litellm_core_utils/litellm_logging.py:2786`) runs `_success_handler_body` inline on the completing thread; the async path likewise. It is still an assumption worth removing, and the redesign no longer depends on it. |
| c | crewai runs the call in a thread without context copy | **No.** The scope in `_crew_call` was opened in the same thread that runs `crew.kickoff()`, so the ContextVar was visible. |
| d | crewai replaces `litellm.callbacks` | **YES � the cause.** |
| e | no fallback when the hook misses | **Yes, contributing.** `CrewOutput.token_usage` is empty on the `is_litellm` path, so nothing covered for it. |

Cause (d), concretely: `crewai/llm.py:704-709` is a
`@model_validator(mode="after")` named `_init_litellm` that runs on **every LLM
construction** and calls `self.set_callbacks(self.callbacks or [])`.
`LLM.set_callbacks` (`crewai/llm.py:2496`) finishes with
`litellm.callbacks = callbacks` (`crewai/llm.py:2519`) � a wholesale
**replacement** of the global list with the LLM's own, which is empty.

`_crew_call` builds a fresh LLM for every role, so the sequence was: install our
logger at startup ? construct the LLM ? CrewAI wipes `litellm.callbacks` ?
`kickoff()` runs with no logger. Preflight escaped this only because its probe
re-asserts the logger *after* building the client and then calls `client.call`
directly, which never re-runs `_init_litellm`.

### The design that does not depend on that

- The hook adds every completion to a **process-global, lock-protected** total
  (`TOTALS`), whatever thread it fires on and whether or not a scope exists.
- `usage_scope(role)` snapshots the totals on entry and reports the **delta** on
  exit, so it does not matter which thread produced the call.
- `install_usage_logger()` is re-asserted immediately before `kickoff()`, after
  the LLM has been constructed and the callback list reset.
- A process lock serialises scoped roles, which is what makes a delta
  unambiguous. The pipeline runs CrewAI roles sequentially in every pattern, so
  this costs no throughput.
- On exit the scope waits briefly (`ATHENA_USAGE_SETTLE_S`, default 2 s) for a
  late async callback before computing the delta.
- If no call was seen but the role returned text, the numbers become an
  **estimate** (`tokens = ceil(chars / 4)`, `"estimated": true`), and the runner
  warns. Real measurements always win.

### Why the callback never fired in the server (Step 6i)

Reproduced offline against a fake OpenAI-compatible provider, real crewai and
real litellm:

```
callbacks before role: ['UsageLogger']
usage scope analyst: calls=0 tokens=101/10 reasoning=0 (estimated)
callbacks after role: ['TokenCalcHandler']
```

- **(b) was not the problem**: the request reached the provider at
  `POST /v1/chat/completions`, so the call does go through
  `litellm.completion`; it is not a native client or a Router.
- **(a)/(d) were the problem**: CrewAI installs its own callback list.
  `LLM.set_callbacks` (`crewai/llm.py:2496`) ends with
  `litellm.callbacks = callbacks` (`crewai/llm.py:2519`) and is called from
  `_init_litellm` (`crewai/llm.py:707`) as well as from the task/agent path
  (`crewai/llm.py:1871`, `crewai/llm.py:2016`). Re-asserting our logger before
  `kickoff()` (Step 6h) is therefore not enough: the list is replaced again
  *during* the call, with `TokenCalcHandler`.
- **(c) was not the cause**: a thread-local scope would have been fine; the
  numbers were missing because no hook ran at all.

**Capture layer chosen: the endpoint-guard wrapper** around
`litellm.completion`/`litellm.acompletion`. It is the one place every call must
pass through, it receives the response object, and it cannot be unregistered by
anything. The `CustomLogger` stays installed as a second pair of eyes, and both
feed the same totals through `record_once()`, which dedupes by response id so a
call seen twice is counted once.

The estimate flag was also being dropped: `run_role_request` copied a fixed list
of ack keys and `estimated` was not in it, so the runner never saw it. It is
copied now, the runner warns, the metrics table marks the row, and the summary
line reports `cost basis: measured` or `cost basis: partly estimated`.

### Output caps and timeouts (Step 6k)

Every model call is clamped in the endpoint-guard wrapper, the one layer all
CrewAI paths cross (plain kickoff, `output_pydantic` through instructor, the
empty-reply retry). The wrapper reads the role recorded by the server
(`athena/config/call_limits.py`, process-level, not a ContextVar) and:

- sets `max_tokens` to the role cap when the request omits it or asks for more;
- sets `timeout` to `ATHENA_LLM_TIMEOUT_S` (default 120) when it is missing or
  larger.

Why it was needed: a critic call returned 131,951 completion tokens (1,335 of
them reasoning) in 589 s. The role cap was 1,500 and the global cap 4,000, but
the request the wrapper saw carried neither, and the A2A timeout was 900 s, so
nothing stopped it. The A2A client now defaults to
`ATHENA_A2A_TIMEOUT_S` = 240 s.

After a call, more than 3x the role cap is flagged as a runaway: an
`error` event with `error_type: runaway_output` is recorded and the role fails
with `RunawayOutputError` (ok:false, no blackboard write).

## Telemetry: cost, heatmap and provenance

The event log stays the single source of truth. Everything below is read-only
shaping of stored events plus the blackboard — nothing calls a model, recomputes
a pipeline value, or fills a gap with a plausible number. When a datum was never
recorded the field is `null`, the list is empty, and a `missing`/`reason` string
says why.

### Cost is a list-price estimate from a cited table

`data/pricing.json` (`ATHENA_PRICING` overrides the path) holds, per model
pattern, input and output USD per million tokens, plus `as_of`, `source`,
`sources[]` and a `note` that states what is *not* modelled (batch, cached
input, off-peak, long-context tiers, free tiers). `athena/observability/cost.py`
prices one call:

```python
from athena.observability.cost import estimate_cost

estimate_cost("groq/llama-3.3-70b-versatile", 1000, 200)   # -> cost_usd, cost_basis="list_price"
estimate_cost("some-new-model", 1000, 200)                  # -> cost_usd=None, cost_basis="unpriced"
```

Matching is case-insensitive: whole id first, then with leading route segments
stripped (`groq/llama-3.3-70b-versatile` → `llama-3.3-70b-versatile`), then glob
in file order, so the most specific pattern must come first. An unknown model, a
missing model name, an unreadable table, or an entry without usable rates all
return `cost_usd=None` with `cost_basis="unpriced"` and a reason. `pricing_meta()`
is what the API states about the basis (`as_of`, `source`, `entries`, currency,
unit) so a number on screen can always be dated and attributed.

**The live default model is deliberately unpriced.** `LLM_MODEL` defaults to
`groq/qwen/qwen3.8-27b`, and that id is not on the public GroqCloud price page
under that name, so no rate can be stated **with a source** for it. It is
therefore listed under `unlisted` in `data/pricing.json` — with `why`,
`checked_as_of`, `checked_source` and the family member that *is* citable
(`qwen/qwen3-32b`, 0.29 in / 0.59 out per Mtok, Groq pricing page) — and it stays
`cost_usd: null`, `cost_basis: "unpriced"`. The `unlisted` block is documentation
only: it is never matched, and `nearest_listed` is never used for arithmetic,
because a different model size is a different model. Add a real `models` entry
the day the provider publishes the id. `pricing_meta()` returns the block as
`unlisted`/`unlisted_note` so the UI can say *why* a run shows no cost.

Totals keep unpriced calls visible instead of treating them as zero:
`cost_basis` is `list_price`, `partly_unpriced`, `unpriced` or `no_model_calls`,
with `priced_calls`, `unpriced_calls` and `unpriced_models` alongside. **Re-read
the provider page before quoting a number and bump `as_of` when you do.**

### Additive event kinds

`athena/observability/events.py` widened `EventKind` only — nothing was renamed
or removed, so rows stored before these existed still validate: `retry`,
`guardrail` and `handoff`.

## Guardrail events: what is implemented, tested and still missing

One row per decision, written by the module that *made* the decision. The shape
is fixed by `athena/observability/guardrail_events.py`:

```json
{"kind": "guardrail", "run_id": "...", "agent": "guardrails", "ts": "...",
 "ok": true,
 "extra": {"stage": "input", "pipeline_stage": "input", "rule": "injection.exfil_request",
           "action": "sanitized", "detail": "system_prompt_exfil: matched 29 character(s)",
           "screen": "screen_input"}}
```

`detail` is passed through `athena.guardrails.secrets.redact_text` (which calls
`athena.observability.redact.redact`) and cut to 200 characters, and the
`Event` validator redacts `extra` a second time. **`detail` never quotes the
payload.** A short message is its own excerpt — `"Ignore all previous
instructions"` is 31 characters — so a "bounded excerpt" would still be the
whole untrusted text. Matches are therefore *measured and dropped*
(`describe_match`: category + character count), and URLs are described by scheme
and length. A test plants a real `GROQ_API_KEY` and asserts it cannot be
reconstructed from the row.

Attribution comes from the active `Recorder` or an operator-supplied run id —
never from the data being screened. `validate_message` in particular writes to
`audit_run_id` only: a forged message carrying `run_id: victim-run` cannot inject
rows into another run's log.

| Stage (`extra.pipeline_stage`) | Emitted by (real call site) | Tested by |
|---|---|---|
| `input` | `guardrails/input.py::screen_input` (one row per flag) | `test_observability_guardrail_events.py::test_input_stage_*` |
| `tool_output` | `guardrails/tool_output.py::validate_tool_output` (default label), called from `chat/service.py` | `...::test_tool_output_stage_*` |
| `evidence` | `tools/adapters.py:53-59` — `screen_items` passes `pipeline_stage="evidence"`; the flag's own stage stays `tool_output` | `...::test_evidence_stage_reports_what_was_dropped` (asserts the label, the rule, and that `extra.stage` is still `tool_output`), `...::test_tool_output_labels_the_evidence_path_separately` |
| `interagent` | `guardrails/interagent.py::validate_message`, called on the real A2A request path at `agents/crew_server.py:195` (after the HMAC at `:187`); rows are written by `interagent.py::emit_rejection:168` | `...::test_interagent_*`, `test_observability_writer_a2a.py::test_a_shape_rejected_a2a_message_is_acked_and_recorded`, `...::test_the_hmac_ran_before_validate_message` |
| `report_sanitize` | `agents/report_sanitize.py:359-366` — `guardrail_flags_for` passes `pipeline_stage="report_sanitize"` to `screen_output` | `...::test_report_sanitize_stage_is_labelled_apart_from_the_chat_answer` |
| `output` | `guardrails/output.py::screen_output` (default), called from `chat/service.py` | `...::test_output_stage_*` |
| `POST /api/runs` | `api/app.py::_record_guardrails` → `guardrail_events.record_flags` (`screen="POST /api/runs"`, `pipeline_stage="input"`); `screen_input` is called with `emit=False` there because the run id does not exist yet | `test_security_integration.py::test_post_runs_records_a_guardrail_event` |
| (all) | `test_telemetry_routes.py::test_guardrail_rows_are_visible_through_the_telemetry_endpoint` — rows reach `/api/runs/{id}/telemetry` as `guardrails`, `guardrail_breakdown` and `/cells` | that test |

All six `pipeline_stage` values are now reachable from production code: `input`
and `tool_output` from the chat/API process, `evidence` from
`tools/adapters.py::screen_items`, `interagent` from the A2A server,
`report_sanitize` from the report sanitiser, and `output` from the chat answer.

### Who is recording: the two processes

A guardrail row is only written when a `Recorder` is active
(`guardrail_events.py::_resolve`). That was true in the research run
(`athena/run.py` installs one) and **false in the chat/API process**, so every
chat decision was silently dropped. `athena/observability/ambient.py` fixes that:

| Piece | Where | What it does |
|---|---|---|
| `install_process_recorder()` | called from `api/app.py:265` (`create_app`) | installs one `ContextRecorder` for the process, and **never** displaces a run's own recorder (`ambient.py:155-157`) |
| `ContextRecorder.emit` | `ambient.py:127-140` | resolves the run id at emit time and stamps `extra.run_id_source`, so two concurrent chat turns cannot overwrite each other's attribution |
| `RunIdScopeMiddleware` | `api/app.py:473`, `ambient.py:189` | puts `conversation_id` from `POST /api/chat/messages` in scope for the request (`RUN_ID_FIELDS`, `ambient.py:169-171`); every other request passes through untouched |
| `UNATTRIBUTED_RUN_ID` | `ambient.py:41` | a row that names no run is written under `unattributed` with `run_id_source="unattributed"`, so it is visible rather than invented |

Tested by `test_observability_ambient.py` (process recorder installed once, a
run's recorder never displaced, `run_scope` attribution, `unattributed` fallback,
middleware replaying the body) and `test_chat_api.py` (a chat turn's guardrail
rows land under its conversation id).

## Retry events: what is implemented, tested and still missing

Shape, from `athena/observability/retry_events.py`:

```json
{"kind": "retry", "run_id": "...", "agent": "writer", "framework": "crewai",
 "ts": "...", "ok": false, "error_type": "empty_reply",
 "extra": {"retry_type": "empty_reply", "attempt": 2, "of": 2,
           "reason": "empty reply: no visible text (finish_reason=length)…",
           "stage": "model_call", "error_type": "empty_reply",
           "token_budget": {"requested": 1000, "retried": 2000}}}
```

`attempt` is 1-based (the first retry after an initial attempt is `2`).
`token_budget` is present only when the retry changed the output budget.
`reason` is redacted and cut to 200 characters. `Event.ts` is the timestamp;
`Event.run_id`/`agent`/`framework` carry the run, role and framework.

**The rule is: never emit a retry that did not happen.** The helpers are called
from the branch that has just decided to re-run something, never from a branch
that suspects it might. Both directions are asserted.

| Retry | Status | Where |
|---|---|---|
| empty reply, budget doubled (ADK) | **Implemented + tested on the real wrapper** — `RateLimitedLiteLlm.generate_content_async` calls `retry_empty_reply("adk", reserve, retried_budget)` at `config/limited_models.py:110`, in the `empty_and_truncated` branch, once per retry | `test_observability_retries.py::test_the_real_empty_reply_retry_never_claims_more_than_one_retry`, `test_reasoning_replies.py::test_empty_reply_is_retried_with_a_doubled_budget` (asserts `seen == [100, 200]` **and** `len(retries) == 1`) |
| empty reply, budget doubled (CrewAI) | **Implemented + tested** — `LLM.call` at `config/limited_models.py:256` and `LLM.acall` at `:285`, same one-row contract | `test_reasoning_replies.py::test_crewai_empty_reply_is_retried` |
| no retry → no event | **Implemented + tested** — the real wrapper driven with a response that *has* text leaves zero `retry` rows | `...::test_an_empty_reply_that_is_not_retried_emits_no_retry_event`, `...::test_a_reply_with_visible_text_is_not_a_retry` |
| `note_empty_reply` is not a retry | **Implemented + tested** — it stays a plain `error` row even though it runs twice | `...::test_noting_an_empty_reply_is_not_itself_a_retry` |
| writer truncated report | **Implemented + tested on the real branch** — `agents/crew_writer.py:259` `retry_truncated_report(agent="writer")`, inside the `looks_truncated` branch, before the re-generation | `test_observability_writer_a2a.py::test_a_truncated_answer_emits_one_retry_event`, `...::test_both_writer_branches_together_emit_exactly_two_retries` |
| writer rejected output | **Implemented + tested on the real branch** — `agents/crew_writer.py:239` `retry_invalid_output(exc, agent="writer")`, in the `except` that re-runs `generate` | `test_observability_writer_a2a.py::test_a_rejected_output_emits_one_retry_event`, `...::test_a_complete_answer_emits_no_retry_event` |
| ADK tool-call rejected by the provider | **Implemented + tested** — `config/limited_models.py:169-179` emits one `retry` (`retry_type="tool_call"`); the `error` row from `tool_call_guard.record_rejection` is the rejection itself, not a second retry | `test_pipeline_tool_call_rejection.py::test_exactly_one_retry_event_is_written_for_that_retry`, `...::test_a_successful_call_writes_no_retry_event` |
| ADK model/tool retry detected from event order | **Implemented** but **inferred by design**, `extra.inferred = true` and `extra.inferred_from` | `test_observability_retries.py::test_inferred_retries_are_marked_as_inferred` |

Every explicit retry above is emitted from the branch that has just decided to
re-run the work, so one retry is one row. The `retry` row duplicates the
`error_type` of the `error` it describes (`note_retry` sets
`error_type=retry_type`); a reader counting failures must filter `kind ==
"error"`, which is what `test_reasoning_replies.py` does.

### Explicit vs inferred, kept as two metrics

`retries` counts `retry` events a call site reported. `retries_inferred` counts
`retry` events carrying `extra.inferred` **plus** `error` events whose
`error_type` is `empty_reply` or `runaway_output` for which no `retry` event
describes the same retry (`athena/api/telemetry_routes.py::Context._index_retries`,
`INFERRED_RETRY_ERRORS`). An `error` that *is* described by a `retry` event is
bucketed `reported` and counted once, not twice. The two totals are never summed,
and `retry_breakdown` names the `retry_type` behind each.

Tested by `test_telemetry_routes.py::test_explicit_and_inferred_retries_are_counted_separately`
and `...::test_an_error_with_no_retry_event_is_reported_as_inferred`.

## Known gaps

Things that are still not wired, stated plainly rather than implied by silence.

| Gap | Status | Why it matters |
|---|---|---|
| `observability/hooks.py::emit_guardrail` | **No production caller.** Only `tests/test_observability_hooks.py` calls it. | Not a hole in the audit trail: every real guardrail module calls `guardrail_events.record_decision`/`record_flags` directly. It is a convenience wrapper kept for future call sites, and it is *not* how the rows in the table above are written. |
| `POST /api/runs` guardrail rows | **Attributed but labelled `input`**, not by the endpoint path. | Screening happens before the run id exists, so `screen_input(emit=False)` is used and `_record_guardrails` writes the rows afterwards. `extra.screen == "POST /api/runs"` is the field that names the endpoint. |
| A2A rejection attribution | **Mostly `unattributed`.** A rejected message's own `run_id` is used only when the blackboard confirms that run exists (`crew_server.py::_rejection_run:98-118`); p2p requests have no blackboard, so they never attribute from the message. | By design — the alternative is letting a forged `run_id` choose whose audit log it writes into. The cost is that most A2A rejection rows are visible under `unattributed` rather than beside the run they concern. `extra.run_id_source` says which case applied. |
| Distributed tracing (OpenTelemetry across the A2A hop) | **Not implemented.** | The A2A hop is visible as `a2a_call` + `handoff` events, not as a trace span with a shared trace id. |
| Event retention / live tail / alerting | **Not implemented.** | The event log grows without bound; there is no TUI tail and no threshold alert. |
| CrewAI token numbers | **Per crew call, not per provider request.** | A crew that retries internally still reports one number, so cost for CrewAI roles is coarser than for ADK. |
| Telemetry for a run that only exists in the chat/API process | **Works, but only for rows written while a `run_scope` was active.** | `GET /api/runs/unattributed/telemetry` shows the unattributable rows; there is no per-conversation telemetry endpoint yet. |

### Endpoints

All four are registered from `athena/api/app.py` via
`athena/api/telemetry_routes.py` and guarded like the rest of `/api`:

- `GET /api/runs/{id}/telemetry` — `agents[]` (`start_ms`/`end_ms`/`duration_ms`
  with `duration_basis`, tokens with `tokens_source`, `cost_usd` + `cost_basis`,
  counts for model/tool/tool-error/error/retry/retries_inferred/guardrail/
  handoff/recovery, `limiter_wait_ms`, `a2a_bytes`, `evidence_contributions`,
  `terminal_state`, `event_ids`), `spans[]` for the waterfall (real events, start
  and end offsets from `ts` and `duration_ms`, `open` when a stage never ended),
  and `totals[]`. Top level also carries `cost_basis` (the pricing metadata) and
  `missing[]`.
- `GET /api/runs/{id}/heatmap.json` — `metric_matrix` (agent × metric) **and**
  `bucket_matrix` (agent × completed stage, in stage order) with `raw`,
  `normalised`, `unit` and the `event_ids` behind every cell; normalisation is
  min–max per column and the basis is stated (`metric_normalisation`,
  `bucket_normalisation`). A bucket cell is the overlap in ms of the agent's own
  **non-stage** spans with that stage window (stage spans are excluded so a stage
  is not counted twice — its duration is the bucket width), and the owner's own
  cell additionally names the bucket's `stage_end` event id first, which is
  exactly the record `/cells?bucket=` returns first. `png_fallback` points at the
  PNG.
- `GET /api/runs/{id}/cells?agent=&metric=` or `?agent=&bucket=` — the real
  stored events behind one cell (an `evidence_items` cell also lists the
  blackboard evidence items). An impossible cell returns `count: 0` with a
  `reason`, never a made-up row.
- `GET /api/runs/{id}/provenance?citation=` (1-based index or a `source_ref`) or
  `?claim=` — `chain[]` hops citation → evidence item → source (URL / document +
  page) → tool invocation → agent → run. `match` says how a reference was
  matched (`exact`, `normalised`, `document`, or a clearly labelled fallback such
  as `unmatched_same_agent_latest`); a claim that no stored evidence contains
  returns `reason` and `missing` instead of a link.

### Frontend

`athena/api/static/telemetry.js` is the pure half (no DOM, `AthenaTelemetry` in
the browser, `require` in node): `shapeTelemetry`, `telemetrySummary`,
`metricGrid`/`bucketGrid`, `heatLevel`/`heatColour`, `waterfallGeometry`,
`cellsUrl`/`eventRows`, `provenanceHops`. `app.js` only builds elements from
those shapes, so the numbers on screen are exactly the ones the API returned:

- two CSS-grid heat matrices in the run details **Metrics** tab (agent × metric,
  agent × stage) with the raw value printed in every cell;
- a waterfall of span bars per agent with a time axis;
- clicking a cell lists the underlying events under the grid (`/cells`), with
  "not recorded" cells disabled rather than blank; a cell with recorded calls but
  no value (an unpriced cost cell) stays clickable, because those calls are the
  reason the price is null;
- loading, empty and failure states are distinct: "loading…", the API's own
  `reason` when there is nothing to chart, and "could not load … <error>";
- the PNG stays available as a *download* fallback (`download PNG`);
- citation chips open a provenance drill-down (`/provenance?citation=N`).

Tests: `tests/test_telemetry_cost.py` (pricing table, matching, unpriced cases,
the deliberately-unlisted live id never pricing anything),
`tests/test_telemetry_routes.py` (shaping, cost, heatmap, cells, provenance,
empty run, new event kinds, bucket-cell/`/cells` consistency, and static wiring:
the Metrics-tab containers, `telemetry.js` loading before `app.js`, the CSS rules
and the four endpoint calls) and
`node athena/api/static/telemetry.test.js` (pure shaping, heat grids, waterfall
geometry, drill-down rows and the `drillable` flag, provenance hops).
