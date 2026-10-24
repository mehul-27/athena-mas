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
- Guardrail decisions are not events yet; Step 7 adds them as their own kind.
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