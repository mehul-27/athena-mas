# Demo

One command, a browser page, and the audience can ask a question and watch the
agents work. No key and no network still works: the page falls back to recorded
runs.

## Run it (Windows `cmd`)

```bat
call .venv\Scripts\activate.bat
set /p LLM_API_KEY=Key: 
cls
set LLM_API_BASE=http://your-gateway.example/v1
set LLM_MODEL=openai/deepseek/deepseek-v4.1-flash
python scripts\demo.py
```

`demo.py` starts the CrewAI A2A server (dev_up), starts the API on
127.0.0.1:8000, opens the browser, and on Ctrl+C stops everything it started.

Without a key, or for a rehearsal:

```bat
python scripts\demo.py --replay-only
```

`--no-browser` prints the URL instead of opening it; `--port` changes 8000.

## Five-minute walkthrough

1. **Header** - the status strip shows the crew server, the model and the
   provider host. `live` means the pipeline will actually run.
2. **Ask** - pick a demo question, choose `sequential`, press Run. Point at the
   **timeline**: agents appear one after another, each tagged ADK or CrewAI.
3. **A2A** - watch the event log: `A2A analyst 165B out / 96B in`. The analyst,
   writer and critic live in a *separate process* reached over A2A on port 9001;
   the bytes are the whole conversation with them.
4. **MCP** - `tool rag_search`, `tool calculate`. The researchers are ADK agents
   calling MCP tool servers over stdio.
5. **Blackboard** - the cards fill in as stages write: task, plan, evidence,
   analysis, draft, critique, final report, with the author and version.
6. **Report** - the structured FinalReport: answer, key points, citations with
   source refs, confidence, and the critic's verdict.
7. **p2p** - run the same question under `p2p` and compare the A2A bytes: the
   p2p pattern carries the context in messages, so its byte count is far higher
   than sequential. That is the architecture trade-off, visible in numbers.
8. **Compare all four patterns** - runs them one after another (never in
   parallel) and fills the table.

## What this demonstrates, and what it does not

| Shown | Not implemented yet |
|---|---|
| Google ADK agents (supervisor, planner, researchers) | Guardrails / input-output safety checks |
| CrewAI roles reached over A2A (analyst, writer, critic) | Distributed tracing (OpenTelemetry) |
| MCP tool servers (RAG, web, calculator) | Evaluation harness and scoring |
| Shared blackboard with history and authorship | Cloud deployment |
| Four orchestration patterns, one registry | Authentication / multi-user |
| Per-agent metrics and heatmap | Cost accounting per run |
| Callback-based monitoring and an event log | Live streaming of partial model output |
