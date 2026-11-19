# Athena

Athena is a local-first AI workspace for everyday chat and multi-agent
research. **Chat** streams replies from the model you choose and can optionally
search the web or use your documents. **Research** coordinates Google ADK and
CrewAI agents to produce a structured report with sources, a critic review,
and an inspectable run trace.

[Quick start](#quick-start) · [Use your own model](#use-your-own-model) ·
[Documents and web search](#documents-and-web-search) ·
[How research works](#how-research-works) · [Troubleshooting](#troubleshooting)

## Quick start

Use **Python 3.13** to match CI and the pinned dependencies. Git is needed to
clone the repository. The app serves its own frontend, so only the frontend
tests need Node.js.

```bash
git clone https://github.com/mehul-27/athena-mas.git
cd athena-mas
```

**Windows PowerShell**

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/demo.py --replay-only
```

**macOS or Linux**

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts/demo.py --replay-only
```

Athena opens [http://127.0.0.1:8000](http://127.0.0.1:8000). From the sidebar,
open **Research → Recordings**, pick a recorded run, and open **Run details** to
inspect its sources and agent activity. Playback makes no model calls and needs
no API key. Press **Ctrl+C** in the terminal to stop Athena.

Run the commands below from the repository root. On Windows, use
`.\.venv\Scripts\python.exe` in
place of `python`. The launcher accepts `--no-browser` and `--port 8001`.

## Use your own model

### Chat

Open **Settings → Add model**. Pick a provider preset, enter the model ID and
provider key, then use **Save & test**. For a custom OpenAI-compatible gateway,
enter its API root (for example, `http://127.0.0.1:8080/v1`). The provider's
model list is the source of truth for the model ID.

Choose the profile in the Chat header. Under **Settings → Models** you can edit
or replace its key, reorder profiles, and choose **Make Chat default**. Use
**Reasoning** beside the composer to override the model's default when the
provider supports it. The launcher can open without a model in `.env`. A profile
saved through Settings can still power Chat.

### Live Research

1. Copy [.env.example](.env.example) to `.env` using
   `Copy-Item .env.example .env` in PowerShell or `cp .env.example .env` on
   macOS/Linux.
2. Set `LLM_MODEL` to a model your provider makes available and `LLM_API_KEY`
   to its key. Use a LiteLLM model string such as `openai/gpt-4o-mini`.
   Set `LLM_API_BASE` if your gateway requires a custom API root.
3. Stop the current launcher and run `python scripts/demo.py` without
   `--replay-only`. The launcher checks both variables before starting the
   local CrewAI server on port **9001**. The browser can open while it starts.
4. Open **Research**, choose a pattern and the tools you need, then submit
   a question. To use a saved profile for live runs, choose **Activate for
   Research** in **Settings → Models**. Activation also starts CrewAI after
   a keyless launch, so this is an alternative to configuring `.env`. If you
   started CrewAI through Settings after a keyless launch, stop it separately
   with `python scripts/dev_down.py` when you are done.

Chat sends the conversation to the selected provider. Research sends the
question and relevant tool output to its active provider. Live runs may make
multiple model calls and incur provider charges.

Keep Athena on loopback for local use. Keep `.env`, provider keys and the
local Settings vault out of Git. See [Security](docs/SECURITY.md) and
[Deployment](docs/DEPLOYMENT.md) for the project's deployment boundaries.

## Documents and web search

### Local documents (RAG)

Open **Documents → Upload document**, or use the upload button beside
Chat's **Documents** control. Athena accepts PDFs with selectable text,
UTF-8 text files, and Markdown. Wait for indexing to finish, then enable
**Documents** in Chat or select the document search tool in Research. Retrieved
excerpts can be sent to the model as context.

For a batch of PDFs, place them in `data/corpus/` and run
`python scripts/ingest_nasa.py`. The default FastEmbed model may download on
first use. To skip that download, set the shell environment variable
`EMBEDDING_PROVIDER=hashing` before running both
the ingestion script and the launcher. In PowerShell use
`$env:EMBEDDING_PROVIDER = "hashing"`. On macOS/Linux use
`export EMBEDDING_PROVIDER=hashing`. The local index uses `data/chroma/` by default.

### Web search

Turn on **Web search** in Chat, or enable web search and page fetching in
Research. The default DuckDuckGo provider needs no key. A self-hosted SearXNG
instance must have JSON output enabled. Set `SEARCH_PROVIDER=searxng` and
`ATHENA_SEARXNG_URL` in `.env`, or select SearXNG in a model profile and enter
the instance URL.

For hosted Firecrawl search:

1. Set `FIRECRAWL_API_KEY` in `.env` or the server environment and restart
   Athena.
2. Select **Firecrawl** in the model profile's **Search provider** field.
   For runs using environment configuration, set `SEARCH_PROVIDER=firecrawl`.
3. Use web search in Chat or Research. Firecrawl receives the search query
   and returns source URLs and snippets. Athena fetches pages with its own
   tool when needed. Chat uses the selected profile and Research uses the
   active profile. The Firecrawl key is separate from the model provider key.

For **self-hosted search**, follow the [Firecrawl Docker Compose guide](https://docs.firecrawl.dev/contributing/self-host)
and publish the unauthenticated API on loopback only. Set
`FIRECRAWL_API_URL=http://127.0.0.1:3002` in Athena's `.env`, restart Athena and
select Firecrawl as above. Athena sends no Cloud key to a local endpoint.
Your Firecrawl server can use a JSON-enabled SearXNG instance via
`SEARXNG_ENDPOINT`. The standalone `firecrawl-local serve` binary forwards
search to Firecrawl Cloud. Use the Compose stack for self-hosted search.

See [.env.example](.env.example) for search, embedding and model settings.

## How research works

Google ADK handles supervision, planning and research. CrewAI runs the
analyst, writer and critic behind an A2A endpoint. Three local MCP servers
provide document search, web search and page fetching, and calculation and
forecasting tools. The persisted SQLite Blackboard holds the plan, evidence,
draft, critique and final report. The peer-to-peer pattern passes context
directly between stages and records an audit trail on the Blackboard.

- **Sequential** runs the stages in order with one researcher.
- **Parallel** runs role-specific research workers concurrently.
- **Hierarchical** assigns work to specialist agents and can retry research
  within a bounded recovery budget.
- **Peer to peer** passes context in A2A stage messages.

Research streams activity as it runs. **Run details** shows the event log,
tool calls, source provenance, and metrics such as agent timing and heatmaps
when available. Chat streams answer text and uses separate conversation storage.
Athena can report evidence gaps, and the critic can reject a report. Review
the cited sources before relying on the result.

For implementation details, see [run flow](docs/FLOW.md),
[Blackboard](docs/BLACKBOARD.md), [patterns](docs/PATTERNS.md), and
[observability](docs/OBSERVABILITY.md). While the live launcher is running,
open the API reference at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

### Evaluation and recordings

The published AMD DeepSeek-V4-Flash batch contains 30 hierarchical runs over
24 distinct prompts and six repeats. All 30 completed, 23 passed the
deterministic rubric, and 17 were approved by the critic. The endpoint was
unpriced, so costs are unknown. These results cover the hierarchical pattern.

- [Evaluation overview](evaluation/REPORT.md)
- [Batch report](evaluation/results/20261114T174056Z_amd_hierarchical_30/BATCH_REPORT.md)
- [Prompts and answers](evaluation/results/20261114T174056Z_amd_hierarchical_30/ALL_RUN_OUTPUTS.md)
- [Per-run metrics](evaluation/results/20261114T174056Z_amd_hierarchical_30/runs.csv)
- [Latency boxplots](evaluation/results/20261114T174056Z_amd_hierarchical_30/charts/latency_by_tier_all_attempts.png)

In the app, open **Research → Recordings** and select an `amd-eval-…` entry
to inspect a saved run without a provider key.

## Troubleshooting

- **Live Research is unavailable.** Check that both `LLM_MODEL` and
  `LLM_API_KEY` are set before starting the launcher. Run
  `python scripts/check_env.py` to check their presence. If CrewAI fails to
  start, check port 9001 and `.run/crew_a2a.log`.
- **The model rejects the request.** Use **Test** in Settings, check the
  provider prefix and model ID, and verify that the API base is the service
  root. For an OpenAI-compatible API, omit the `/chat/completions` suffix.
- **Document search is empty.** Check the indexed files under **Documents**.
  Upload a readable file and make sure document search is enabled for the
  request. Scanned PDFs need OCR before upload.
- **Search is empty or throttled.** Retry or change the search provider.
  Research may report an evidence gap when it cannot retrieve usable sources.
- **Port 8000 is busy.** Start with `python scripts/demo.py --port 8001`.
- **Research returns HTTP 409.** Wait for the current live run to finish.
  The local launcher supports one live Research run at a time.

## Development

- Python tests: `python -m pytest -q`. The default excludes live end-to-end
  tests that require credentials.
- Frontend tests: with Node.js, run the `*.test.js` files in
  `athena/api/static/`. The exact CI commands are in the
  [workflow](.github/workflows/ci.yml).
- Local state: Chat and Research use SQLite files under `data/`. The RAG
  index is under `data/chroma/`. Saved model profiles and their encrypted
  vault are under `data/settings/`, and run logs are under `.run/`.

## License

This repository currently has no project-wide license. Vendored assets carry
their own licenses in their respective directories.
