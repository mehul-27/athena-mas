# Athena project guide

Athena is a local-first AI workspace. It combines a direct model-backed chat, multi-agent research, a local document library, and personal follow-up tools in one interface. The browser app runs on a local Python API. The optional Windows desktop shell opens that same interface in a dedicated window.

This guide describes the current `athena-mas` project. Athena is designed for local use. Keep it bound to `127.0.0.1` and keep API keys, `.env`, and local settings out of Git.

## Quick start

The project targets Python 3.13. From the repository root, create an environment and install the pinned dependencies:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/demo.py --replay-only
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Replay-only starts Athena without a provider key and lets you inspect a recorded research run. Press `Ctrl+C` in the terminal to stop it. The launcher accepts `--no-browser` and `--port 8001`.

For live chat, add a model and API key under **Settings → Add model**. To run live Research, activate a saved profile with **Activate for Research**, or configure `LLM_MODEL` and `LLM_API_KEY` in `.env`. Live chat and research send prompts to the selected provider, and can incur provider charges.

## Windows desktop app

The Windows app uses Tauri and WebView2 to display Athena's existing interface. The desktop shell starts the Python API in the background and uses the checkout's `data/` directory for local state. Closing the main window hides it in the system tray. The tray menu provides **Open Athena** and **Quit Athena**. While Athena is running, the desktop shell checks for overdue reminders and new watchlist results and requests Windows tray notifications for them.

Build from the repository root with Rust, Microsoft C++ Build Tools, and WebView2 installed:

```powershell
cargo build --locked --release --manifest-path desktop/Cargo.toml --target x86_64-pc-windows-msvc
.\desktop\target\x86_64-pc-windows-msvc\release\Athena.exe
```

Keep the executable in the checkout, or set `ATHENA_DESKTOP_ROOT` to the repository path. For live Research in the desktop app, use **Activate for Research** in **Settings → Models**. Chat and recorded runs do not require starting the research worker.

## Main features

### Chat

Chat streams responses from the selected model profile. Each conversation has its own saved history and model choice. You can opt into web search and selected local document context for a message, and select tools for individual messages. The reasoning control can override the model's reasoning effort when the provider supports it.

### Multi-agent Research

Research turns a question into a structured report with sourced evidence, a draft, and critic feedback. Runs stream their activity to the interface and save an inspectable event trace and run state.

Athena supports several research patterns:

- **Sequential** runs the research stages in order with one researcher.
- **Parallel** runs role-specific research workers concurrently.
- **Hierarchical** delegates tasks to specialist agents and can retry within a bounded recovery budget.
- **Peer to peer** passes context directly between stages through agent messages.

The run details view displays source provenance, tool activity, stage history, and available run metrics. Completed reports have a **Visual report** view with a dark theme option and browser printing to PDF. Research can flag evidence gaps or return a report rejected by the critic, so check the cited sources before relying on a result.

### Documents

The local document library indexes PDFs with selectable text, UTF-8 text, and Markdown. Upload from **Documents** or from Chat's document control. Once indexed, excerpts can be retrieved as context for a chat or Research run. Scanned PDFs need OCR before ingestion. The index is local and defaults to `data/chroma/`.

For batch PDF ingestion, put files in `data/corpus/` and run `scripts/ingest_nasa.py`. The default embedding model may download when first used. Setting `EMBEDDING_PROVIDER=hashing` avoids that model download.

### Model profiles and search

Add and manage provider profiles in **Settings**. A profile stores a provider preset, model ID, API base URL, reasoning preference, and an encrypted API key. Profiles can be tested, reordered in Chat's model menu, and assigned as the Chat default. Research uses the active research profile.

Available web search options include keyless DuckDuckGo, a self-hosted SearXNG instance, and hosted or local Firecrawl. Configure provider keys or endpoints in the profile or server environment as appropriate. Firecrawl search credentials are separate from model credentials. See `.env.example` and the main README for setup details.

### Projects

Projects group related chats, completed research reports, indexed documents, reminders, watchlists, and reviewable actions. Each project has editable notes. A chat in a project can use the project's notes and relevant recent messages from other chats in that same project as context. Select **All chats** to leave the project scope.

### Personal memory and continuity

Under **Settings → Personalisation**, edit an About Me description and manage saved memories about you, your preferences, and ongoing goals. You can add, edit, or forget individual memories and disable automatic learning. When learning is enabled, Athena saves selected explicit profile and preference statements from chat. Secret-shaped content is excluded.

Memory is stored locally. Relevant memories are included in requests to your configured model. Resuming an existing chat restores its conversation history. Athena can also retrieve relevant older messages from that same chat, while project conversations can share bounded recent context within their project.

### Saved workflows

**Workflows** are reusable prompt recipes. Create or edit a workflow and use `{{input}}` where a question or topic should be supplied each time. Selecting a workflow fills the Chat composer with a draft. Review or change it, then press **Send**. You can also save a chat response as a workflow.

### Follow-ups

The **Follow-ups** panel contains reminders, watchlists, and reviewable action drafts.

- **Reminders** store a title, local due time, and optional notes. When Athena is running, overdue reminders can trigger a Windows notification.
- **Watchlists** periodically search for changes while Athena is running and retain recent updates. New watchlist results can trigger a Windows notification.
- **Review actions** store a note, email draft, or calendar event for review. Approving an item records the approval locally. Approved notes can be downloaded as Markdown, and approved calendar events can be exported as `.ics` files.

These features and their records stay local to the Athena data directory.

### Evaluation and recordings

**Research → Recordings** lists saved runs that can be replayed and inspected without making new model calls. Open a run's details to inspect recorded activity and evidence. The **Evaluation** view appears when evaluation results are available. The repository also includes evaluation datasets, scoring and analysis code, reports, and run outputs under `evaluation/`.

The current published benchmark report summarizes one 30-run hierarchical batch. Its results apply to that dataset and configuration. See [evaluation/REPORT.md](evaluation/REPORT.md) for the report and links to the run-level results.

## How the project is organized

- `athena/api/` contains the FastAPI application and HTTP routes for chat, research, settings, the document library, projects, memory, workflows, follow-ups, and evaluation.
- `athena/api/static/` contains the browser interface and its JavaScript, styles, and frontend tests.
- `athena/chat/` contains chat storage, provider calls, context construction, and personal memory.
- `athena/agents/`, `athena/patterns/`, and `athena/blackboard/` implement research agents, run patterns, and shared durable run state.
- `athena/tools/` contains document retrieval, web search and fetching, MCP servers, calculation, and forecasting.
- `athena/observability/` records run events, traces, redacted summaries, and metrics.
- `desktop/` contains the Tauri shell and its Windows startup page.
- `evaluation/` contains benchmark datasets, evaluation code, reports, and saved results.
- `scripts/` contains launch, demo, ingestion, diagnostics, and local service helpers.
- `data/` contains local runtime state such as chat and research databases, settings, follow-ups, workflows, and the document index. `.run/` contains local service logs.

The browser interface and Windows app use the same local FastAPI service. Chat calls a model directly through the selected profile. Research is coordinated by Google ADK and uses CrewAI agents through an A2A endpoint. Local MCP servers expose selected research tools. A persisted SQLite Blackboard stores research state and the stages' shared evidence and drafts.

## Further reading

- [README.md](README.md) for provider setup, search configuration, and troubleshooting.
- [docs/FLOW.md](docs/FLOW.md), [docs/PATTERNS.md](docs/PATTERNS.md), and [docs/BLACKBOARD.md](docs/BLACKBOARD.md) for the research pipeline.
- [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) for run tracing and metrics.
- [docs/SECURITY.md](docs/SECURITY.md) and [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for security and deployment boundaries.
- [evaluation/REPORT.md](evaluation/REPORT.md) for benchmark results.

## Tests

Run the Python test suite from the project root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Frontend tests use Node.js and are in `athena/api/static/*.test.js`. The CI workflow lists the exact project checks in `.github/workflows/ci.yml`.
