# Athena Agents

One baseline pipeline: **planner → researcher → analyst → writer → critic**.
It is sequential and fixed. Configurable patterns, guardrails and tracing come
later (Steps 5–9) and are deliberately absent here.

## Agents

| Agent | Framework | Reads | Writes | Tools |
|---|---|---|---|---|
| planner | Google ADK (`LlmAgent`) | `task` | `plan` | `write_plan` (a local function tool) |
| researcher | Google ADK (`LlmAgent`) | `task` | `evidence` (append) | `rag_search`, `web_search`, `calculate`, `forecast` — three MCP servers over stdio |
| analyst | CrewAI (A2A on `:9001`) | `task`, `evidence` | `analysis` | none |
| writer | CrewAI (A2A on `:9001`) | `task`, `plan`, `evidence`, `analysis` | `draft`, `final_report` | none |
| critic | CrewAI (A2A on `:9001`) | `task`, `evidence`, `draft`, `final_report` | `critique` | none |

Authors on the blackboard are exactly `planner`, `researcher`, `analyst`,
`writer`, `critic`. The only other author is `system` for the `create` entry
that `create_run` writes.

The supervisor is a small custom ADK `BaseAgent` that runs the five stages in
order. It exists instead of `SequentialAgent` because it must also time each
stage and return the final report; control returns to it after every stage and
`transfer_to_agent` is never used.

## A2A message contract

The message body is the entire contract — nothing else travels:

```json
{"run_id": "…", "role": "analyst|writer|critic", "instruction": "≤300 chars"}
```

Rules:

- `instruction` is truncated to 300 characters; the payload is capped at
  **600 bytes** and is normally ~120.
- The server validates the JSON, the role, and that the `run_id` exists. A bad
  request returns `{"ok": false, "error": "…"}` — it does not crash.
- The reply is a short JSON acknowledgement, never content:

```json
{"ok": true, "role": "writer", "section": "final_report", "version": 1}
```

- The writer adds `"fallback": true` when its generation failed twice and it
  wrote the minimal report instead.

Everything else — evidence, drafts, the report — stays in the blackboard, which
both processes open at `BLACKBOARD_DB`.

## One run, in order

1. `python scripts/dev_up.py` starts the CrewAI A2A server (`:9001`).
2. `python -m athena.run "<question>"` creates the run (`task` section).
3. **planner** calls `write_plan` → `plan` (author `planner`).
4. **researcher** calls MCP tools; after each call the `evidence_recorder` hook
   appends the real result to `evidence` (author `researcher`). The model never
   copies tool output.
5. **analyst** (A2A → CrewAI) reads task + evidence, writes `analysis`.
6. **writer** (A2A → CrewAI) writes `draft` then `final_report`. Citations are
   filtered to real evidence `source_ref`s (RAG refs carry the page, e.g.
   `handbook.pdf#p11-chunk-28`) and `tools_used` is derived from the evidence.
7. **critic** (A2A → CrewAI) writes `critique`. It does not trigger a rewrite.
8. The supervisor reads `final_report` and the runner prints it with the
   blackboard history.

## Recording rule for failed tools

A tool call that failed — `ok=false` from the tool, or an MCP-level `isError` —
records **no** evidence. A tool error is not a source; the model still sees the
error in its own tool response and can retry. This is implemented in
`athena/agents/evidence_recorder.py` and tested.

## Reliability

- `LLM_MAX_TOKENS_<ROLE>` overrides the per-role budget (planner 400,
  researcher 600, analyst 500, writer 1500, critic 400).
- Both model factories set `num_retries=3` so LiteLLM backs off on rate limits
  instead of failing immediately.
- The writer retries once with the validation error appended; if that fails it
  writes a minimal valid `FinalReport` (confidence `0.0`) so the pipeline still
  completes. The runner prints a `writer fallback used` line when this happens.
## Rate limiting (Step 5)

Both processes share one SQLite-backed sliding window
(`athena/config/ratelimit.py`). Every model call reserves its estimated input
tokens and its `max_tokens` before it runs, and settles the real usage after:

- `LLM_ITPM_LIMIT` � input tokens per minute (0 disables).
- `LLM_OTPM_LIMIT` � output tokens per minute. Providers count the request's
  `max_tokens`, so a 1500-token writer cannot share a minute with anything
  else when the plan allows only 1000.
- `LLM_RATELIMIT_MAX_WAIT_S` � how long a call may wait before it fails with
  `RateLimitWaitError` instead of hanging.

The hook is the narrowest point both frameworks share: `LiteLlm`'s
`generate_content_async` for ADK and `LLM.call`/`acall` for CrewAI. The two
worker-side guards are `before_tool_callback` (refuses a tool call past the
budget) and `after_tool_callback` (records what the tool returned).
## Reasoning models (Step 6b)

- `LLM_REASONING_EFFORT` (`low|medium|high`, empty = not sent) is passed to ADK's
  `LiteLlm` (via its extra completion kwargs) and to `crewai.LLM` (a native
  field, so it also covers the `output_pydantic` path).
- Per-role output budgets are planner 1500, researcher 2000, analyst 1500,
  writer 3000, critic 1500; `LLM_MAX_TOKENS_<ROLE>` overrides any of them.
- An empty reply with `finish_reason=length` is retried once with the budget
  doubled (cap 4000) and logged as an `empty_reply` error event; the second
  empty reply is reported, not retried again.
- Reasoning tokens are counted separately from visible output in the event log
  and the metrics.

## When a CrewAI role fails (Step 6e)

A role either produces its section or reports failure � never a placeholder.

- **No placeholder writes.** A failed analyst or critic writes nothing to its
  blackboard section. Before Step 6e they caught the exception, wrote
  `gaps=["analysis generation failed: TypeError"]` (or a `medium` critique) and
  returned success, which is why the ack said `ok:true` while the crew had
  produced zero tokens.
- **The ack carries the failure.** `{"ok": false, "role", "error_type",
  "error": <200 chars, redacted>}`, and the server emits an `error` event with
  the same type and message.
- **The run is marked degraded.** The runner prints
  `WARNING <role> failed: <type>: <msg>` and records `<role>: <type>` in
  `RunSummary.stage_errors`, which sets `degraded`, which shows in the summary
  line, the `run_summary` event and the `--compare` table.
- **Continuing is allowed, silently succeeding is not.** The pipeline continues
  when the next stage can still work from what the blackboard already holds:
  a missing analysis is readable (the writer's prompt says
  "(no analysis yet)"), so the writer and critic still run; a missing evidence
  section would leave nothing to cite, and the writer falls back to the minimal
  report. Either way the run is degraded and says so.
- **A successful call always spends tokens.** An `ok:true` ack with
  `tokens_in == 0` and `tokens_out == 0` is treated as suspicious and the runner
  prints `WARNING <role> reported zero tokens`.

## Usage accounting is serialised (Step 6h)

Token usage for CrewAI roles is a **delta of process-global counters**, because
CrewAI resets `litellm.callbacks` whenever it builds an LLM and the callback can
fire on any thread. A delta is only meaningful if one scoped role runs at a
time, so `usage_scope` holds a process lock for the duration of a role call.

Consequence: two A2A requests to the same CrewAI server are serialised for the
length of the role. The pipeline runs CrewAI roles sequentially in every
pattern (sequential, parallel � only the researchers run concurrently, and they
are ADK-side � hierarchical and p2p), so this costs no throughput today. If a
future pattern needs concurrent CrewAI roles, the accounting must move to a
per-call id (for example the `litellm` call's `metadata`), not to a global
delta.