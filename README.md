# Athena

Athena is a local-first, multi-agent research assistant built with **Google ADK**,
**CrewAI**, **A2A**, **MCP**, and a persistent **Blackboard**. Ask a research
question, watch the agents work, inspect the evidence and critique, and compare
four orchestration patterns on the same question.

The UI is a chat-style research console: it shows the run timeline, A2A traffic,
MCP tool calls, the shared Blackboard, per-agent metrics, the structured final
report, and the critic's verdict. If no provider key is configured, it still
runs from recorded demo bundles.

Repository: <https://github.com/mehul-27/athena-mas>

> **Local use only.** Athena has no authentication or tenant isolation. Do not
> expose the demo API to the public internet.

---

## What it does

- Decomposes a question into a plan.
- Researches through MCP tools: local RAG, web search, page fetching, and
  calculation/forecasting.
- Records every piece of evidence with provenance and authorship on the
  Blackboard.
- Produces an analysis, a structured `FinalReport`, and a critic review.
- Rejects unsupported answers instead of inventing findings or citations.
- Shows evidence gaps explicitly when the retrieved sources cannot support the
  requested claims.
- Supports `sequential`, `parallel`, `hierarchical`, and `p2p` orchestration.
- Streams progress over Server-Sent Events (SSE), including safe reconnection.
- Persists run history, event metrics, heatmaps, logs, and final summaries.

---

## Architecture

```text
Browser
  │
  ▼
FastAPI demo API  ────────────────►  Blackboard (SQLite)
  │                                  task / plan / evidence / draft /
  │                                  critique / final_report + history
  │
  ├── ADK supervisor + planner + researchers
  │       │
  │       └── MCP stdio servers
  │             ├── rag_search
  │             ├── web_search
  │             ├── fetch_page
  │             └── calculate / forecast
  │
  └── A2A (127.0.0.1:9001)
          └── CrewAI roles: analyst, writer, critic
                    │
                    └── read/write the same Blackboard
```

- **ADK** owns the supervisor, planner, and researchers.
- **CrewAI** owns the analyst, writer, and critic behind the A2A server.
- **A2A** carries the run ID and stage instructions between processes.
- **MCP** exposes the research tools as stdio servers.
- **Blackboard** is the durable, append-only-history state store. The `p2p`
  pattern carries context in messages and writes audit records afterwards.

Only the two required agent frameworks are used; there is no third agent
framework or separate orchestration library.

---

## Requirements

- **Python 3.11+** (developed and tested on Python 3.13).
- Windows, macOS, or Linux.
- Internet access for live web search, page fetching, and hosted LLM calls.
- No GPU required. RAG can use a local/fastembed model or the offline `hashing`
  embedding provider.

The demo is a single-user local application. All state is stored under the
repository directory.

---

## Quick start

### 1. Clone

```bash
git clone https://github.com/mehul-27/athena-mas.git
cd athena-mas
```

### 2. Create a virtual environment

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

**macOS / Linux**

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

All commands below assume the repository root as the working directory. On
Windows, prefix commands with `.venv\Scripts\`; on macOS/Linux, activate the
virtual environment once and use `python`.

### 3. Configure a provider

Copy the example file:

```powershell
copy .env.example .env
```

```bash
cp .env.example .env
```

Edit `.env` and set at least:

```dotenv
LLM_MODEL=groq/qwen/qwen3.8-27b
LLM_API_KEY=your-key-here
```

Do **not** commit `.env`. It is already ignored by `.gitignore`.

### 4. Start Athena

```powershell
.\.venv\Scripts\python scripts\demo.py
```

```bash
python scripts/demo.py
```

This starts:

- the CrewAI A2A server on `127.0.0.1:9001`,
- the demo API and UI on `http://127.0.0.1:8000`,
- your default browser.

Press `Ctrl+C` to stop everything the launcher started.

### 5. No key? Use replay mode

```powershell
.\.venv\Scripts\python scripts\demo.py --replay-only
```

```bash
python scripts/demo.py --replay-only
```

Replay mode needs no provider, network, or API key. It plays the recorded
bundles in `data/demo/replay/`. Other options:

```text
--port 8001       use a different API port
--no-browser      print the URL instead of opening a browser
```

---

## Provider configuration

### Model string format

Athena uses LiteLLM-style model strings:

```text
<provider>/<model-id>
```

Examples:

```dotenv
LLM_MODEL=groq/qwen/qwen3.8-27b
LLM_MODEL=deepseek/deepseek-chat
LLM_MODEL=openai/gpt-4o-mini
LLM_MODEL=openrouter/anthropic/claude-3.5-sonnet
```

For an OpenAI-compatible gateway:

```dotenv
LLM_MODEL=openai/your-model-name
LLM_API_BASE=http://127.0.0.1:8080/v1
LLM_API_KEY=your-gateway-key
```

`LLM_API_BASE` must be the API root, not a `/chat/completions` URL.

### Environment variables

The most important settings:

| Variable | Purpose |
|---|---|
| `LLM_MODEL` | LiteLLM-style provider/model string. |
| `LLM_API_KEY` | Provider API key. Never commit it. |
| `LLM_API_BASE` | Optional OpenAI-compatible API root. |
| `LLM_REASONING_EFFORT` | Optional `low`, `medium`, or `high`. |
| `LLM_MAX_TOKENS_<ROLE>` | Per-role output caps. |
| `ATHENA_MAX_REVISIONS` | Hierarchical recovery cycles after a high rejection. |
| `ATHENA_RESEARCHER_MAX_TOOL_CALLS` | Researcher tool-call budget (default 8). |
| `ATHENA_RESEARCHER_MAX_MODEL_CALLS` | Researcher model-call budget (default 10). |
| `SEARCH_PROVIDER` | `duckduckgo`, `searxng`, or `disabled`. |
| `SEARXNG_INSTANCE` | Base URL for self-hosted SearXNG. |
| `EMBEDDING_PROVIDER` | `fastembed` (default) or `hashing`. |
| `CORPUS_DIR` | Local PDF corpus directory. |
| `CHROMA_PATH` | Local Chroma index directory. |
| `BLACKBOARD_DB` | Blackboard SQLite path. |
| `EVENTS_DB` | Event log SQLite path. |

See [.env.example](.env.example) for the complete list and defaults.

### Settings UI alternative

After starting the app, open **Settings** in the UI. You can create a provider
profile with:

- model string,
- API base URL,
- API key,
- reasoning effort,
- search provider.

Keys saved through Settings are stored in the local encrypted vault under
`data/settings/`, which is ignored by Git. The app checks only whether a key is
present; it never prints the value. **Test connection** validates the profile
without displaying the secret.

The active profile can override `.env` values for subsequent runs. If you are
unsure which configuration is in use, check the header/status strip in the UI.

---

## Running the pipeline directly

The demo API is convenient, but the CLI is useful for scripts and debugging.

### 1. Start the A2A server

```powershell
.\.venv\Scripts\python scripts\dev_up.py
```

```bash
python scripts/dev_up.py
```

This writes `.run/crew_a2a.pid` and `.run/crew_a2a.log` and waits for the Agent
Card on port 9001.

### 2. Run a question

```powershell
.\.venv\Scripts\python -m athena.run "Explain three key changes in Python 3.13 with official sources." --pattern hierarchical
```

```bash
python -m athena.run "Explain three key changes in Python 3.13 with official sources." --pattern hierarchical
```

Options:

```text
--pattern sequential|parallel|hierarchical|p2p
--compare                 run every pattern on the same question
--eval-id N               use a question from data/eval/nasa_rag_40_questions.csv
--run-id <id>             use a supplied run id
--json-summary <path>     write the RunSummary as JSON
```

### 3. Stop the A2A server

```powershell
.\.venv\Scripts\python scripts\dev_down.py
```

```bash
python scripts/dev_down.py
```

The `demo.py` launcher does this automatically on exit.

---

## Orchestration patterns

Choose a pattern in the UI or with `--pattern`.

| Pattern | How it works | Research workers | Recovery |
|---|---|---|---|
| `sequential` | planner → researcher → analyst → writer → critic | one researcher with all tools | evidence gate; no research revision loop |
| `parallel` | planner → role-specific researchers concurrently → analyst → writer → critic | RAG, web, calc workers named by the plan | evidence gate; no research revision loop |
| `hierarchical` | planner dispatches plan steps by `assigned_to`; writer/critic run as a loop | role-specific researchers | high-severity critic rejection can trigger one targeted research recovery plus a writer revision |
| `p2p` | stages exchange context in A2A messages; supervisor audits to Blackboard afterwards | one researcher with all tools | evidence gate; no research revision loop |

All four produce the same Blackboard sections and comparable final reports.

The `hierarchical` loop is bounded by `ATHENA_MAX_REVISIONS` (default `1`). A
missing critique is never treated as approval. If recovery budget is exhausted,
the result remains rejected/incomplete instead of being marked successful.

---

## How research and evidence work

Athena does not simply ask the writer to “be accurate.” The pipeline has several
independent checks:

1. **Source routing** chooses local RAG for supplied-document questions and
   web/official documentation for software, API, release, and current-fact
   questions.
2. **Search snippets are hints, not evidence.** The researcher must fetch
   relevant pages before claims can rely on them.
3. **Evidence is recorded with provenance**: source type, source reference,
   retrieved-by role, and content. Failed or empty fetches are kept for audit
   but excluded from citable evidence.
4. **The sufficiency gate** checks requested finding counts, distinct sources,
   official/primary-source requirements, relevance, and whether content was
   actually fetched.
5. **The writer is blocked** from producing an unsupported “complete” answer.
   It writes a gap report with no citations and confidence `0.0` when the
   evidence is insufficient.
6. **The critic reviews** the report, evidence, citations, and gaps. In
   `hierarchical`, an evidence-related high rejection can request new research.

The gate is intentionally conservative but heuristic. It cannot prove that a
particular sentence is semantically supported by a page; the critic and the
writer prompts still matter. See [Limitations](#limitations).

---

## RAG setup

RAG is optional. Web research works without it.

1. Put one or more PDFs in `data/corpus/` (the NASA systems-engineering handbook
   is the example corpus; the PDF is not committed).
2. Build the local index:

```powershell
.\.venv\Scripts\python scripts\ingest_nasa.py
```

```bash
python scripts/ingest_nasa.py
```

For a fully offline index with no embedding-model download:

```powershell
.\.venv\Scripts\python scripts\ingest_nasa.py hashing
```

```bash
python scripts/ingest_nasa.py hashing
```

The index is written to `data/chroma/`. Re-running is safe: unchanged chunks are
skipped. If the corpus is unrelated to the question, the researcher should not
treat its chunks as evidence.

RAG-related environment variables:

```dotenv
EMBEDDING_PROVIDER=fastembed
CORPUS_DIR=data/corpus
CHROMA_PATH=data/chroma
```

---

## Web search and page fetching

The default search provider is keyless DuckDuckGo HTML search:

```dotenv
SEARCH_PROVIDER=duckduckgo
```

It can be throttled. For steadier local use, run SearXNG:

```bash
docker run -d --name searxng -p 8888:8080 searxng/searxng
```

```dotenv
SEARCH_PROVIDER=searxng
SEARXNG_INSTANCE=http://127.0.0.1:8888
```

`SEARCH_PROVIDER=disabled` turns web search off without breaking local RAG or
calculation questions.

Page fetching is performed by the existing `fetch_page` MCP tool. It applies
SSRF protections, redirect validation, a total deadline, response-size limits,
and untrusted-content boundaries. Fetched page text is treated as data, not
instructions.

Useful limits:

```dotenv
WEB_FETCH_TIMEOUT=12
WEB_FETCH_TOTAL_TIMEOUT=20
WEB_FETCH_MAX_BYTES=1000000
WEB_FETCH_MAX_CHARS=8000
```

---

## Web API

The demo server listens on `127.0.0.1:8000` by default.

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | App, provider, and crew-server status. |
| `GET /api/questions` | Curated demo questions. |
| `POST /api/runs` | Start a run (`{"question": "...", "pattern": "sequential"}`). |
| `GET /api/runs` | Run history and summaries. |
| `GET /api/runs/{run_id}` | Blackboard snapshot for a run. |
| `GET /api/runs/{run_id}/stream` | SSE progress stream. |
| `GET /api/runs/{run_id}/log` | Process log and termination watchdog state. |
| `GET /api/runs/{run_id}/metrics` | Per-agent metrics. |
| `GET /api/runs/{run_id}/heatmap.png` | Rendered metrics heatmap. |
| `GET /api/compare?run=id1,id2` | Comparison rows. |
| `GET /api/replay` | Recorded demo bundles. |
| `/api/settings/*` | Provider profiles, activation, and connection test. |

SSE clients receive:

- unnamed `data:` events for timeline/event records;
- `event: blackboard` for Blackboard updates;
- `event: done` with a terminal `summary`;
- `event: error` if the stream times out.

Events carry sequence IDs and the stream honors `Last-Event-ID`, so a client can
resume without replaying already-seen events. If a run cannot be recovered after
the stream has started, Athena emits a terminal **failed** summary rather than
raising an HTTP error into an already-open response.

Only one live run is allowed at a time. A second start returns HTTP 409 while the
active run is still working.

---

## Testing

Offline tests are the default and make no live network/provider calls:

```powershell
.\.venv\Scripts\python -m pytest -q
```

```bash
python -m pytest -q
```

The FastAPI tests, pipeline tests, event-store tests, Blackboard tests, MCP tool
tests, and streaming regressions are included.

Frontend logic tests need Node.js:

```bash
node athena/api/static/logic.test.js
node athena/api/static/app.composer.test.js
```

Opt-in end-to-end tests require a live provider and/or running A2A server:

```bash
python -m pytest -m e2e
```

Do not run e2e tests without provider credentials and explicit intent; they make
real provider calls.

Useful environment checks:

```powershell
.\.venv\Scripts\python scripts\check_env.py
.\.venv\Scripts\python scripts\preflight.py
```

`check_env.py` reports whether variables are present, never their values.

---

## Local data and storage

Everything is local by default:

| Path | Contents |
|---|---|
| `data/blackboard.db` | Blackboard sections and append-only history. |
| `data/events.db` | Append-only observability event log. |
| `.run/runs/` | Per-run logs and `.summary.json` files. |
| `.run/crew_a2a.pid` / `.log` | CrewAI A2A server process and log. |
| `data/chroma/` | Local RAG vector index. |
| `data/corpus/` | Local source PDFs (not committed). |
| `data/settings/` | Encrypted profile vault; ignored by Git. |
| `data/demo/replay/` | Recorded demo bundles (committed). |

These paths are ignored by `.gitignore` where appropriate. The event log and
Blackboard history are append-only by design; there is no automatic retention
policy. Delete the local databases only when you intentionally want to reset
history.

---

## Troubleshooting

### The browser opens, but the run immediately uses replay mode

No provider key was found. Check `.env` or the Settings profile. Confirm the
presence (not the value) with:

```powershell
.\.venv\Scripts\python scripts\check_env.py
```

### “CrewAI A2A server is not answering on 127.0.0.1:9001”

Start it manually:

```powershell
.\.venv\Scripts\python scripts\dev_up.py
```

Inspect `.run/crew_a2a.log`. Port 9001 must be free. Stop it with
`python scripts\dev_down.py`.

### HTTP 409 when starting a run

Athena allows one live run at a time. Wait for the current run to finish, or
stop the previous server process. Do not start a second live run concurrently.

### Provider `401`, `403`, `404`, or “model not found”

- Check the model string provider prefix.
- Check that `LLM_API_BASE` is the API root, not a full completion URL.
- Some gateways require an `openai/` prefix for OpenAI-compatible models.
- Re-test the profile in Settings.

### RAG returns no useful chunks

- Confirm PDFs exist in `data/corpus/`.
- Re-run `python scripts/ingest_nasa.py`.
- For a corpus unrelated to the question, Athena should ignore it rather than
  force it into the answer.
- Try `EMBEDDING_PROVIDER=hashing` only for offline debugging; quality is lower.

### Web search is throttled or empty

DuckDuckGo can rate-limit. Switch to a self-hosted SearXNG instance, retry later,
or use local RAG/calculation tools. An empty search or failed fetch is not
treated as evidence.

### SSE reconnects

A freshly accepted run may not have written its Blackboard task for a few
seconds. The stream waits for a valid task instead of returning a 404 into an
open SSE response. If the run is genuinely gone, the UI receives a terminal
failed summary. If the stream simply times out, the server emits an `error`
event; reconnect or check `/log`.

### Port 8000 is already in use

```powershell
.\.venv\Scripts\python scripts\demo.py --port 8001
```

```bash
python scripts/demo.py --port 8001
```

---

## Security notes

- `.env` and `data/settings/` are ignored. Never commit API keys.
- The Settings vault encrypts stored provider keys; the local master key is also
  ignored.
- The app checks key presence without printing values.
- Fetched web content is untrusted external data and is wrapped with explicit
  boundaries for the model prompts.
- `fetch_page` validates URLs, blocks private/internal targets, follows redirects
  safely, and enforces time and size limits.
- The demo API has **no authentication**. Keep it bound to `127.0.0.1`.
- Do not paste real keys into issues, logs, screenshots, or recorded bundles.

---

## Repository map

```text
athena/
  agents/          ADK/CrewAI roles, A2A stage, evidence, routing, sufficiency
  api/             FastAPI app, run registry, SSE, replay, settings routes
  blackboard/      Pydantic schemas, SQLite store, validation
  config/          LLM factories, caps, rate limiting, endpoint guard
  observability/   events, metrics, heatmap, redaction, recording
  patterns/        sequential, parallel, hierarchical, p2p
  settings/        provider profiles and encrypted vault
  tools/           MCP servers and tool implementations
  api/static/      browser UI (HTML/CSS/vanilla JS)
scripts/           demo launcher, A2A lifecycle, ingestion, diagnostics
tests/             offline pytest suite and streaming regressions
data/
  corpus/          local PDFs (not committed)
  eval/            small evaluation CSVs (committed)
  demo/replay/     recorded demo runs (committed)
docs/              architecture and design notes
```

---

## Design notes

- [Blackboard](docs/BLACKBOARD.md)
- [Patterns](docs/PATTERNS.md)
- [Demo walkthrough](docs/DEMO.md)
- [Observability](docs/OBSERVABILITY.md)
- [Agent roles](docs/AGENTS.md)

Some documents describe earlier stages of the build; the README above reflects
the current runnable system.

---

## Limitations

- **Single-user / local-first.** There is no authentication, authorization, or
  tenant isolation. SQLite is single-host.
- **One live run at a time** in the demo registry.
- **Recovery is pattern-dependent.** `hierarchical` supports bounded research
  recovery; `sequential`, `parallel`, and `p2p` stop with a gap/rejected result
  when evidence is insufficient.
- **Evidence sufficiency is heuristic.** It checks fetched sources, relevance,
  distinctness, official-source policy, and content length, but cannot fully
  prove claim-level semantic support. The critic remains the final semantic
  check.
- **Search quality is provider-dependent.** Keyless DuckDuckGo can throttle;
  SearXNG is more reliable if you run one.
- **RAG quality depends on the corpus.** An unrelated corpus is rejected as
  evidence rather than forced into the answer.
- **No partial token streaming.** The UI streams pipeline events and status, not
  model tokens.
- **No cloud deployment, multi-user mode, cost accounting, or OpenTelemetry
  tracing** yet.
- **The UI is intentionally lightweight vanilla HTML/CSS/JS**, not a separate
  frontend application.

---

## License

No license file is included in this repository. Add one before distributing or
reusing the code outside its intended coursework/demo context.
