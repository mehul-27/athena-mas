# Athena

A research assistant with **Chat**, **Web Search** and **RAG** (retrieval over a
document corpus), rebuilt as a **multi-agent system** for a college course.

The product idea is unchanged; the architecture is not. Athena is built on
exactly two agent frameworks — **Google ADK** and **CrewAI** — talking to each
other over **A2A**, and exposing tools over **MCP**.

## Status

This repository is at the **skeleton + interop spike** stage, with the shared
blackboard, output schemas and the MCP tool servers in place. No product
features exist yet; the goal right now is to prove that ADK and CrewAI can talk
to each other, that MCP tools work, and that separate agent processes can share
state.

- `athena/blackboard/` — shared state store and the Pydantic schemas.
- `athena/tools/` — three MCP tool servers: RAG search, web search, calculate/forecast.
- `athena/` — the rest of the package (empty subpackages, reserved by design).
- `spike/` — throwaway proof-of-concept code for the Stage 1–3 checkpoints.
- `docs/GUIDELINES.md` — the 11 mandatory project rules and their status.

See [docs/GUIDELINES.md](docs/GUIDELINES.md) for the rules checklist and
[docs/BLACKBOARD.md](docs/BLACKBOARD.md) for the shared-state design.

## Tools (MCP)

Three stdio servers, each returning a typed Pydantic result rather than a raw
dict, and reporting failures as `ok=false` instead of raising:

```powershell
.venv\Scripts\python scripts\ingest_nasa.py    # build data/chroma from data/corpus
.venv\Scripts\python scripts\demo_tools.py     # call all three servers over stdio
```

| Server | Tools | Env |
|---|---|---|
| `athena/tools/rag_server.py` | `rag_search` | `CHROMA_PATH`, `CORPUS_DIR`, `EMBEDDING_PROVIDER` |
| `athena/tools/web_server.py` | `web_search` | `SEARCH_PROVIDER` (duckduckgo / searxng / disabled) |
| `athena/tools/calc_server.py` | `calculate`, `forecast` | — |

`EMBEDDING_PROVIDER=hashing` runs the RAG path with no model download, which is
what the offline tests use. The NASA corpus PDF is not committed; put it in
`data/corpus/` (the eval CSVs in `data/eval/` are).

## Layout

```
athena/            python package (agents, tools, blackboard, guardrails,
                   observability, evaluation, config)
spike/             Stage 1-3 interop code
tests/             offline tests (e2e tests are opt-in)
docs/              guidelines and design notes
data/              document corpus placeholder
```

## Setup

Requires Python 3.11+ (developed on 3.13).

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then fill in LLM_API_KEY
```

`athena/config/llm.py` reads `LLM_MODEL` (a LiteLLM-style model string) and
`LLM_API_KEY` from `.env` and hands back framework-native model objects.

## Tests

```powershell
.venv\Scripts\python -m pytest          # offline only
.venv\Scripts\python -m pytest -m e2e   # needs a live LLM / running server
```

## Spike checkpoints

| Stage | What it proves | Entry point |
|-------|----------------|-------------|
| 1 | A CrewAI agent served over A2A | `spike/crew_a2a_server.py` |
| 2 | An ADK agent delegating to it over A2A | `spike/run_spike.py` |
| 3 | Adding an MCP tool to the ADK agent | `spike/run_spike.py` |

### Running the spike

Two terminals, both from the repo root:

```powershell
# Terminal 1 - the CrewAI analyst, served over A2A on port 9001
.venv\Scripts\python spike\crew_a2a_server.py

# Terminal 2 - the ADK supervisor delegating to it over A2A
.venv\Scripts\python spike\run_spike.py
```

The Agent Card is served at
`http://127.0.0.1:9001/.well-known/agent-card.json`. Card URLs use `127.0.0.1`
rather than `localhost` on purpose: ADK requires the card's RPC URL to share an
origin with the URL the card was fetched from.

## Demo

```bat
call .venv\Scripts\activate.bat
set /p LLM_API_KEY=Key: 
cls
set LLM_API_BASE=http://your-gateway.example/v1
set LLM_MODEL=openai/deepseek/deepseek-v4.1-flash
python scripts\demo.py
```

One command starts the CrewAI A2A server, the demo API on 127.0.0.1:8000 and
the browser, and Ctrl+C stops everything it started. Without a key:
`python scripts\demo.py --replay-only` plays the recorded runs in
`data/demo/replay/`. See `docs/DEMO.md` for the walkthrough and the honest list
of what is not implemented yet.

### Re-record the demo

The bundles in `data/demo/replay/` are recordings. To refresh them after a
change (four real runs, about 4-5 minutes):

```bat
call .venv\Scripts\activate.bat
set /p LLM_API_KEY=Key: 
cls
set LLM_API_BASE=http://your-gateway.example/v1
set LLM_MODEL=openai/deepseek/deepseek-v4.1-flash
python scripts\record_demo.py --overwrite
```

Without `--overwrite` the new bundles are written as `<pattern>-new.json`, so the
shipped ones stay untouched until you have looked at them. Runs that are
degraded or below 0.5 confidence are not exported unless you pass `--keep-bad`.