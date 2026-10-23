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
