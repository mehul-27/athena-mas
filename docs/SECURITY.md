# Security: threat model, controls, tests and limits

Athena is a multi-agent research assistant. It fetches third-party web pages,
reads an operator-supplied document corpus, runs model-authored tool calls, and
splits the pipeline across processes that talk over HTTP and SQLite. That means
untrusted text reaches the model from four directions (the user, a document, a
web page, a tool result), and untrusted *messages* reach three more (the A2A
port, the blackboard, the chat API).

This document states what is defended, where the control lives, which test
proves it, and what is deliberately **not** defended. The guardrail layer is
`athena/guardrails/`: small, deterministic, rule-based, configurable by
environment variables, and cheap enough to run on every message.

## Threat model

| # | Threat | Entry point | Control | Code | Test |
|---|---|---|---|---|---|
| 1 | Malicious prompt: jailbreak, persona switch ("DAN"), policy override, system-prompt exfiltration | Chat message, task question | Lexical screening with flags; directives stripped before the model sees the message; only harmful-content categories block | `guardrails/input.py`, `guardrails/injection.py` | `test_security_adversarial.py::test_manipulative_*` |
| 2 | Injection inside a fetched web page or search snippet | `web_search`, `fetch_page` results | Result wrapped in the untrusted-content boundary; boundary markers inside it removed; instruction-like spans flagged; injected phrasing recorded so an echoing answer is caught | `agents/untrusted.py`, `guardrails/tool_output.py`, `chat/grounding.py` | `test_security_adversarial.py::test_web_snippet_*` |
| 3 | Injection inside an uploaded document (RAG passage, PDF text) | `rag_search` hits, corpus ingest | Same boundary + same flagging, per source block; budget cuts drop whole blocks so a boundary is never left unterminated | `chat/grounding.py`, `chat/prompts.py` | `test_security_adversarial.py::test_document_injection_*`, `test_document_injection_does_not_truncate_a_boundary` |
| 4 | Malicious or malformed MCP/tool output | Tool callbacks, evidence recorder, chat tool loop | `validate_tool_output`: byte cap (64 KiB), UTF-8/control-character sanity, URL scheme and host sanity, embedded-instruction flagging; never raises | `guardrails/tool_output.py` | `test_security_adversarial.py::test_tool_abuse_*` |
| 5 | Malformed or forged inter-agent message | A2A/JSON-RPC port, p2p payloads | Explicit mode enum (`p2p`/`blackboard`), role allowlist, field allowlist, size bounds, HMAC-SHA256 over `run_id` + body with constant-time compare; unsigned messages warn (or fail, with `ATHENA_A2A_REQUIRE_SIG=1`). Both halves run on the real request path: the HMAC at `agents/crew_server.py:185`, then `validate_message` at `:195` | `guardrails/interagent.py` | `test_security_guardrails.py::test_validate_message_*`, `test_peer_auth_*`, `test_observability_writer_a2a.py::test_a_shape_rejected_a2a_message_is_acked_and_recorded`, `...::test_the_hmac_ran_before_validate_message` |
| 6 | Unsupported claims presented as fact | Writer output | Evidence-bound citation sanitising, deterministic claim support check, confidence ceilings | `agents/report_sanitize.py`, `agents/claim_support.py`, `agents/critique_rules.py` | `tests/test_report_sanitize.py` |
| 7 | Toxic or abusive content from the user, or reproduced by the model | Chat message, final answer | Lexicon with severity: flagged as `warned`, never blocking by default (a rude question with a real request is still answered); high-severity blocking is opt-in | `guardrails/input.py`, `guardrails/output.py` | `test_security_adversarial.py::test_hostile_*`, `test_security_guardrails.py::test_screen_output_flags_toxicity_in_the_answer` |
| 8 | Secret leakage (keys pasted into chat, tokens echoed by a tool or the model, keys in an event or API response) | Chat store, transcript, tool payloads, report text, event log | `redact_tree` over stored/returned structures plus `observability.redact`; PEM blocks, JWTs, `sk-`/`ghp_`/`xox`/`gsk_` prefixes, `Authorization`/`Bearer` headers, URL credentials | `guardrails/secrets.py`, `chat/store.py` | `test_security_adversarial.py::test_secret_*`, `test_transcript_never_renders_a_secret`, `test_pem_private_key_is_redacted` |
| 9 | Unauthorized tool use (unknown tool, disabled capability, query-injection into a tool) | Chat tool loop, planner tool selection | Tools are resolved against a fixed catalogue and current availability before execution; unknown names are reported and never dispatched; calculator uses an AST walk, not `eval`; no `shell=True`, `eval`, `exec` or `pickle.loads` anywhere in `athena/**` | `chat/tools.py`, `tools/registry.py`, `tools/calc.py` | `tests/test_chat_api.py::test_unknown_tool_is_rejected_and_never_executed`, `test_security_adversarial.py::test_athena_package_has_no_dynamic_execution_or_shell` |
| 10 | Resource exhaustion (huge page, huge transcript, tool loop spin, oversized tool output) | Fetch, RAG, chat history, tool loop, inter-agent context | Fetch byte/char caps, RAG top-k, chat message/history/turn budgets, tool-call and round caps, tool-output byte cap, p2p context cap | `tools/web/fetch.py`, `chat/service.py`, `guardrails/tool_output.py`, `guardrails/interagent.py` | `test_security_adversarial.py::test_tool_abuse_oversized_payload_is_rejected`, `test_tool_abuse_cap_is_configurable` |

## Controls in one paragraph

Every chat turn is screened once, before anything is created or stored. A
verdict carries `GuardrailFlag` rows (`athena.blackboard.schemas`), a `blocking`
boolean and the categories that fired. **Only the short harm list can block** —
self-harm instructions, targeted violence, weapon/drug synthesis, sexual content
involving minors, hands-on cyber abuse — and only when the wording shows no
research intent (`policy on…`, `how to prevent…`, `statistics for…`). Everything
else is flagged and neutralised: the model is given `legit_remainder(message)`,
which keeps the real question and drops the directive, so a manipulative message
with a legitimate question still gets an answer. Toxic wording is flagged with a
severity and never blocks unless `ATHENA_GUARDRAILS_BLOCK_TOXIC=1`.

Retrieved text is never rewritten — it is evidence — but every block is rendered
inside `----- BEGIN UNTRUSTED EXTERNAL CONTENT (DATA ONLY) -----`, any copy of a
boundary marker inside it is replaced, and the accompanying rule (the same
`UNTRUSTED_DATA_RULE` the research pipeline uses) tells the model the block is
data. Instruction-like spans found in that text are recorded, so the final
answer can be checked for repeating them verbatim (`output.obeyed_injection`).

On the way out, `screen_output` produces flags for secret-shaped text, toxicity,
leaked boundary markers and obeyed injection; it remains flag-only. The report
caller uses `redact_final_report` before draft/final-report writes to record safe
flags and recursively redact report strings, including citation and flag fields.
This redaction still runs when guardrail screening is disabled. It is
shape-based; it does not rewrite source evidence or historical rows.

## Environment switches

| Variable | Default | Effect |
|---|---|---|
| `ATHENA_GUARDRAILS_ENABLED` | `1` | `0` makes every guardrail a no-op (screening returns no flags) |
| `ATHENA_GUARDRAILS_BLOCK` | `1` | `0` downgrades every `blocked` verdict to `warned` |
| `ATHENA_GUARDRAILS_BLOCK_TOXIC` | `0` | `1` also blocks high-severity toxicity |
| `ATHENA_GUARDRAILS_MAX_INPUT_CHARS` | `8000` | input text screened per message |
| `ATHENA_GUARDRAILS_TOOL_MAX_BYTES` | `65536` | byte cap for one tool result |
| `ATHENA_GUARDRAILS_TOOL_ALLOW_LOCAL` | `0` | `1` permits loopback/private hosts in tool output |
| `ATHENA_A2A_SHARED_TOKEN` | generated | shared HMAC secret for inter-agent messages |
| `ATHENA_A2A_REQUIRE_SIG` | `0` | `1` rejects unsigned inter-agent messages |
| `ATHENA_RAG_MAX_FILE_BYTES` | `52428800` | largest PDF accepted by ingest (50 MiB) |
| `ATHENA_RAG_MAX_PAGES` | `2000` | largest page count accepted by ingest |
| `ATHENA_RAG_MAX_TEXT_CHARS` | `2000000` | largest plain-text document accepted by ingest |

`ATHENA_A2A_SHARED_TOKEN` is the one stateful control: when it is unset,
`PeerAuth.token` generates a random token with `secrets.token_urlsafe(32)` and
writes it into `os.environ`, so every child process started by this one inherits
the **same** token instead of inventing a different one and failing every call.
It is therefore process-tree scoped, not machine scoped: set it in `.env` when
the A2A server runs as a separate service. The value is never logged.

## Integration: where each control is called

The guardrails package is not a library sitting beside the pipeline; every
control below is invoked on the real path, and each has a test in
`tests/test_security_integration.py`.

| Call site | Control | Behaviour |
|---|---|---|
| `chat/service.py::prepare_turn` | `screen_input` | flags stored on the message `meta`; the model is handed `legit_remainder`; a blocking verdict refuses with 422 and a non-echoing message |
| `chat/service.py` (tool loop) | `validate_tool_output` | flags added to the turn's `guardrail_flags`, the result is used as data only |
| `chat/grounding.py`, `chat/prompts.py` | `wrap_untrusted`, `scan_injection` | every retrieved block rendered inside the boundary with the pipeline's `UNTRUSTED_DATA_RULE` (no duplicated rule); hits recorded on the grounded turn |
| `chat/store.py` | `redact_text`, `redact_tree` | message content, title, sources, tool events and meta are redacted on write *and* on read |
| `chat/service.py` (final answer) | `screen_output` | flags stored on the assistant message |
| `agents/report_sanitize.py` | removal counts, `screen_output`, `redact_final_report` | flags are derived, then report text, citations, tools and flags are redacted before draft/final writes; secret-shaped citation refs are dropped. Redaction is shape-based, not a guarantee for every possible secret |
| `agents/crew_server.py` | `peer_auth_error` | with `ATHENA_A2A_SHARED_TOKEN` set, a signed body must verify or the role is never dispatched; unsigned bodies are allowed unless `ATHENA_A2A_REQUIRE_SIG=1`; with no token the single-host flow is unchanged |
| `agents/a2a_client.py` | `sign_message` | the outgoing payload is signed only when a token is configured (a client never invents one, so it cannot sign itself out of a working deployment) |
| `agents/evidence_recorder.py` / `tools/adapters.py` | `validate_tool_output` → `screen_items` | an item that is invalid as data (over the byte cap, undecodable, uninspectable) is dropped and reported back to the model as `dropped_by_guardrails` + `guardrail_flags`; an item that merely *contains* an instruction is recorded and flagged, because evidence must not be silently lost |
| `api/app.py` | `screen_input` on `POST /api/runs` | the question is screened before a run starts; flags use `screen="POST /api/runs"` and are returned in the response; only `verdict.blocking` produces a 422 |
| `run.py::_execute` | `screen_input` | direct CLI execution re-screens before task creation/model input; flags use `screen="athena.run"`; blocked input gets a failed terminal summary without an A2A health probe or model run |
| `api/app.py::_snapshot` | `redact_tree` | every blackboard section (`final_report`, `draft`, `analysis`, `plan`, `critique`, `task`) is redacted field by field before it is returned |
| `tools/rag/ingest.py` | size/page caps | a PDF over `ATHENA_RAG_MAX_FILE_BYTES`/`ATHENA_RAG_MAX_PAGES`, or text over `ATHENA_RAG_MAX_TEXT_CHARS`, is refused (and counted in the corpus stats) instead of being truncated and half-indexed |

## Guardrail audit rows: implemented, tested, partial

Every guardrail decision this package makes is also written as one `guardrail`
event, through `athena/observability/guardrail_events.py`. The row carries the
run id (from the recorder, never from the data screened), the stage, the rule,
the action and a redacted `detail`; it never carries the payload. Full schema and
per-stage table: `docs/OBSERVABILITY.md`.

| Control | Emits a row | Tested |
|---|---|---|
| `screen_input` (chat, API, CLI) | yes, one per flag per screening boundary, stage `input`. API attribution is `POST /api/runs`; execution attribution is `athena.run` | `test_observability_guardrail_events.py::test_input_stage_*`, `test_security_integration.py::test_post_runs_records_a_guardrail_event`, `tests/test_run_input_guardrails.py` |
| `validate_tool_output` / `screen_items` | yes, one per flag. Default stage `tool_output`; `tools/adapters.py:53-59` passes `pipeline_stage="evidence"`, so production evidence rows say `evidence` while the flag's own `stage` stays `tool_output` | `...::test_tool_output_stage_*`, `...::test_evidence_stage_reports_what_was_dropped` |
| `validate_message` (A2A/p2p) | yes, one per rejection and one per flag, stage `interagent`, attributed to `audit_run_id` only. **Called on the real path** at `agents/crew_server.py:195`, after the HMAC at `:185` | `...::test_interagent_*` (incl. `test_interagent_attribution_ignores_the_run_id_in_the_message`), `test_observability_writer_a2a.py::test_a_shape_rejected_a2a_message_is_acked_and_recorded`, `...::test_the_hmac_ran_before_validate_message` |
| `screen_output` (chat answer, report sanitiser) | yes, one per flag, stage `output`; report callers then redact before persistence | `...::test_output_stage_*`, `...::test_report_sanitize_stage_is_labelled_apart_from_the_chat_answer`, `tests/test_report_secret_redaction.py` |
| `agents/crew_server.py::peer_auth_error` + `_record_rejection` | **yes** — an HMAC refusal writes one `interagent.signature` row (`crew_server.py:127-137`), so the A2A server's rejections are no longer silent | `test_observability_writer_a2a.py::test_the_hmac_ran_before_validate_message` |
| Rows visible through `GET /api/runs/{id}/telemetry` and `/cells` | yes | `test_telemetry_routes.py::test_guardrail_rows_are_visible_through_the_telemetry_endpoint` |

`detail` deliberately does not quote the matched text. A short message is its own
excerpt (`"Ignore all previous instructions"` is 31 characters), so the previous
"bounded excerpt" was the payload whenever the match spanned the input. Matches
are measured and dropped (`describe_match`: category + character count); URLs are
described by scheme and length. Month-old rows may still contain an excerpt;
new rows cannot.

## Implemented, tested, and what is still partial

### Implemented and tested

| Control | Real call site | Test |
|---|---|---|
| Input screening | `guardrails/input.py::screen_input` ← `api/app.py` (`POST /api/runs`), `run.py::_execute`, `chat/service.py` | `test_security_adversarial.py::test_manipulative_*`, `test_security_guardrails.py`, `tests/test_run_input_guardrails.py` |
| Tool-output validation on the evidence path | `guardrails/tool_output.py::validate_tool_output` ← `tools/adapters.py:53` (`screen_items`) | `test_security_adversarial.py::test_tool_abuse_*`, `test_observability_guardrail_events.py::test_evidence_stage_reports_what_was_dropped` |
| Inter-agent HMAC (outbound + inbound) | `guardrails/interagent.py::PeerAuth`, `sign_message` ← `agents/a2a_client.py`; verified at `agents/crew_server.py:185` (`peer_auth_error:87`) | `test_security_guardrails.py::test_peer_auth_*`, `test_security_integration.py:225-290` |
| Inter-agent message validation (mode enum, role/field allowlist, instruction/context size bounds) | `guardrails/interagent.py::validate_message` ← `agents/crew_server.py:195`, with `check_signature=False` because the HMAC was already decided at `:185` | `test_security_guardrails.py` (validation unit tests), `test_observability_writer_a2a.py::test_a_shape_rejected_a2a_message_is_acked_and_recorded` |
| Guardrail rows survive in the chat/API process | `observability/ambient.py::install_process_recorder` ← `api/app.py:265`; `RunIdScopeMiddleware` ← `api/app.py:473` | `test_observability_ambient.py` (17 tests), `test_chat_api.py` |
| Report flags and secret redaction | `agents/report_sanitize.py::sanitize_report` / `redact_final_report`; writer paths scrub drafts and reports before SQLite writes | `test_observability_guardrail_events.py::test_report_sanitize_stage_is_labelled_apart_from_the_chat_answer`, `tests/test_report_secret_redaction.py` |

### Partial / known gaps

1. **A2A rejection attribution is usually `unattributed`.** The HMAC and the shape
   checks both run, but the row's run id may only come from the message when the
   blackboard confirms that run exists (`crew_server.py::_rejection_run:98-118`).
   A p2p message has no blackboard, so it never attributes from its own content —
   which is the point, since a forged `run_id` must not choose its audit log.
   `extra.run_id_source` records which case applied, so the gap is visible rather
   than silently absorbed.
2. **API and execution input rows describe separate screenings.** The API checks
   before run creation; the CLI execution boundary checks again before model input.
   Their `extra.screen` values distinguish those decisions (`POST /api/runs` and
   `athena.run`).
3. **No distributed tracing across the A2A hop**, no event retention, no
   alerting. See `docs/OBSERVABILITY.md` ("Known gaps").
4. **Toxicity and harm lexicons are small by design** (see the limits section at
   the end of this document).

## Tests

- `tests/test_security_adversarial.py` — six attacker personas (hostile,
  manipulative, injection-in-document, injection-in-web-snippet,
  secret-exfiltration, tool-abuse), 38 cases, each asserting a concrete
  behaviour: which rule fired, whether the turn was refused, exactly what the
  model was given, whether a secret survived into storage or a transcript, and
  whether a spoofed boundary marker was neutralised. It also contains the static
  check that `athena/**` contains no `shell=True`, `eval(`, `exec(` or
  `pickle.loads(`.
- `tests/test_security_guardrails.py` — unit tests for the modules and the chat
  wiring: switches, HMAC sign/verify, message validation, output screening, and
  the end-to-end assertion that a streamed turn hands the model the legitimate
  remainder and records the flags.
- `tests/test_security_integration.py` — the call sites in the table above:
  report flags, A2A signing/verification (including the unsigned dev flow),
  tool-output validation in the evidence path, `POST /api/runs` screening and
  recording, snapshot redaction, and the ingest caps.
- `tests/test_observability_guardrail_events.py` — the audit rows themselves:
  one redacted row per decision for each stage, a real secret planted in the
  payload and asserted absent from the row, a forged inter-agent `run_id` that
  must not write into another run's log, and "a clean screening writes nothing".
- `tests/test_telemetry_routes.py` — that those rows are visible through
  `GET /api/runs/{id}/telemetry` (counts, `guardrail_breakdown`, `/cells`), and
  that explicit and inferred retries land in separate metrics.
- `tests/test_untrusted_content.py` (existing) — the research pipeline's
  boundary markers, which the chat path now reuses instead of duplicating.

Run them with:

```bash
python -m pytest tests/test_security_adversarial.py tests/test_security_guardrails.py \
    tests/test_security_integration.py tests/test_chat_api.py \
    tests/test_untrusted_content.py -q
```

## Honest limitations

- **This is lexical, not semantic.** A paraphrase, a translation, a directive
  split across sentences, a base64 payload or an instruction in an image will
  not match these patterns. There is no ML classifier and no second-model judge:
  a determined attacker will get text past `screen_input`.
- **Prompt injection cannot be solved in the prompt.** The boundary marker and
  the data rule raise the cost of an attack and make the model's job easier;
  they do not make injection impossible. The only real defences are the ones
  outside the model: tools that cannot act on instruction text, citations that
  must be backed by recorded evidence, and a human who reads the flags.
- **The boundary is advisory to the model.** `wrap_untrusted` removes spoofed
  markers and delimits the region; it cannot stop a model that decides to obey
  the text anyway. `output.obeyed_injection` is a tripwire that catches the
  crudest case (the attacker's phrase reappearing verbatim), not every case.
- **Secrets are matched by shape.** A key in a format nobody has seen before,
  or one assembled from fragments, will pass. The environment-value pass only
  knows the variables in *this* process. Redaction is a safety net, not a
  permission to paste keys into chat.
- **Toxicity and harm lexicons are small on purpose.** They will miss most abuse
  and can misfire on quoted text. That is why they flag rather than gate: a
  false positive costs an audit row, not a refused request, except in the short
  harm list where a false negative is worse.
- **Inter-agent HMAC is only as good as the token's distribution.** Unsigned
  messages warn by default so an existing deployment keeps working; set
  `ATHENA_A2A_REQUIRE_SIG=1` before exposing the A2A port beyond localhost. The
  token is process-tree scoped unless set explicitly.
- **The signature is not a network control.** It authenticates the *message*
  (whoever holds the token), not the sender's identity, and it says nothing
  about a compromised peer process that holds the same token. It stops a local
  process or a browser that can reach the port from posting a role request; it
  does not replace TLS or network isolation.
- **The blackboard and the event log trust local processes.** Anyone who can
  write `data/blackboard.db` can forge evidence. Isolation is the operating
  system's job (file permissions, one user per deployment), not this layer's.
- **No sandbox.** Tools run in-process with the application's privileges; the
  guardrails bound *what text they accept*, not the damage a compromised tool
  implementation could do.
- **Snapshot redaction is shape-based.** `redact_tree` removes strings it
  recognises as secrets; a value that looks like ordinary prose is returned
  unchanged. It is a last line of defence over stored data, not an authorization
  boundary on the API — see `docs/DEPLOYMENT.md` for the access controls.
- **Ingest limits are per document, not per corpus.** A directory of many
  admissible documents is still ingested in full; the caps bound one file's
  size and page count, not the total work of a corpus build.

