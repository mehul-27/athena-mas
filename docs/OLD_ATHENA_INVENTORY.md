# Old Athena inventory (read-only, `M:\Athena\athena`)

A local copy, newer than the public repo. Listing only: no `.env`, no `data/`
contents were opened. Sizes are line counts of Python files (bytes for assets).

## 1. Deep research

| Path | Lines | What it does |
|---|---|---|
| `backend/research/engine.py` | 638 | `DeepResearcher`: plans sub-questions, runs search rounds, decides when to stop |
| `backend/research/runner.py` | 403 | `ResearchRunner`, `format_research_report`: drives the engine, assembles the markdown |
| `backend/research/prompts.py` | 137 | planner / summariser / follow-up prompts |
| `backend/research/relevance.py` | 110 | scores and filters sources per round |
| `backend/research/json_utils.py` | 100 | tolerant JSON extraction from model output |
| `backend/research/quality.py` | 23 | quality gate on the draft |
| `backend/research/sources.py` | 33 | source normalisation |
| `backend/research/store.py` | 77 | per-run persistence |
| `backend/research/llm.py` | 79 | LLM call wrapper for the research loop |
| `backend/api/research.py` | 143 | HTTP endpoints |

Flow: question → plan (sub-questions) → N search rounds → fetch and score pages →
summarise per round → relevance filter → iterate until the plan is covered or a
round/step budget is hit → final markdown report. Prompts are summarised, not
copied, in this document.

Third-party: `httpx`, `beautifulsoup4`, the provider layer. **Offline: no** (needs
an LLM and search; the search/fetch layer is stubbable). **Port effort: L** — it
depends on the provider layer, the search layer and the report schema.

## 2. Visual report

| Path | Lines / bytes | What it does |
|---|---|---|
| `backend/research/report/renderer.py` | 1738 | Markdown → single-file HTML: heading ids, TOC, autolinking, image injection, category CSS |
| `backend/research/report/schema.py` | 410 | report section schema |
| `backend/research/report/generator.py` | 177 | builds the report structure from the research result |
| `backend/research/report/prompts.py` | 71 | section-writing prompts |
| `backend/api/…` | - | serves the rendered HTML |
| `frontend/report.js` / `report-view.html` | 18,872 B / 2,416 B | the viewer |

Format: **self-contained HTML** (inline CSS, no CDN, no chart library; images are
remote URLs pulled from search results). Sample output in the repo:
`presentation/Athena_Project_Presentation.pdf` and `exports/athena_chats.md`.
**Offline: yes** once the markdown exists — it is pure string/HTML work.
**Port effort: L** for the full renderer (1738 lines), **S–M** for a subset
(sections + citations + inline CSS).

## 3. Search providers and page fetch

| Path | Bytes | Notes |
|---|---|---|
| `search/web.py` | 9,057 | keyless DuckDuckGo HTML scraping, SearXNG JSON, result cleaning |
| `search/fetch.py` | 5,357 | page fetch with an **SSRF guard**: `assert_public_url` resolves host IPs and rejects private ranges |
| `search/news.py` | 1,591 | recency search, SearXNG or recency-filtered web |
| `backend/providers/*` | 1,000+ | keyed providers (registry, router, health, pricing) |

**Offline: no** (network); the SSRF guard is unit-testable. **Port effort: S–M**
(we already have keyless DuckDuckGo + SearXNG + throttling; the SSRF guard is the
piece worth porting).

## 4. Settings, providers, key storage

| Path | Lines | Notes |
|---|---|---|
| `backend/security/secrets.py` | 137 | `SecretBox`: **Fernet** with a single key file under `data/`, `enc:` value prefix, `_restrict(path)` to lock the key file down |
| `backend/providers/store.py` | 453 | provider records, active provider, encrypted keys |
| `backend/providers/registry.py` | 339 | provider catalog |
| `backend/providers/presets.py` | 206 | presets (Command Code, Groq, OpenRouter, OpenAI, custom) |
| `backend/providers/urls.py` | 123 | URL validation |
| `frontend/settings.js` | 37,829 B | the settings screen |

Key storage approach: a Fernet master key file on disk, values encrypted at rest
with an `enc:` marker; keys never returned by the API. **Offline: yes.**
**Port effort: M** — this step's vault follows the same model but prefers an OS
keystore where one is available.

## 5. Documents upload and RAG UI

| Path | Lines | Notes |
|---|---|---|
| `backend/services/documents.py` | 162 | `DocumentService`: safe filenames, size limit, registry file, ingest via `rag.ingestion.ingest_pdf` |
| `backend/api/rag.py` | 79 | upload/list/delete endpoints |
| `frontend/app.js` | 42,758 B | upload UI lives here |

PDF only in the old app; size limit enforced in `DocumentService`. **Offline:
yes** for the registry, no for embedding unless a local model is used.
**Port effort: M** — this step's version adds TXT/MD and per-document toggles.

## 6. Calendar

| Path | Lines | Notes |
|---|---|---|
| `backend/api/calendar.py` | 322 | REST endpoints |
| `backend/calendar/store.py` | 380 | SQLite store, RRULE expansion, .ics import/export |
| `backend/calendar/dates.py` | 131 | date parsing helpers |
| `frontend/calendar.js` | 31,121 B | month/week UI |

Dependencies: `python-dateutil` (RRULE) and `icalendar` (.ics), both in the old
`requirements.txt`. **Offline: yes.** **Port effort: L** (store + UI + agent
actions); not planned for this rebuild.

## 7. What makes it a personal assistant

`backend/conversations/store.py` (380), `backend/chat/capabilities.py` (355),
`backend/mcp/manager.py` (341) + `store.py` (291), `backend/tools/{loop,policy,
registry,execution,native}.py`, `backend/api/{chat,conversations,prefs,mcp}.py`,
the evaluation harness (`nasa_experiment.py` 712, `nasa_report.py` 1093,
`judge.py` 307), provider routing/health/pricing, and the presentation deck.

**Port effort by area:** conversations S–M, MCP registry M, tools policy M,
evaluation L, presentation S (assets only).

## Summary for planning

| Area | Effort | Depends on |
|---|---|---|
| Deep research loop | L | provider layer, search, report schema |
| Visual report | L (S–M for a subset) | research output |
| Search + SSRF-safe fetch | S–M | httpx, bs4 |
| Settings + encrypted keys | M | cryptography Fernet |
| Documents + RAG | M | pypdf, chromadb |
| Calendar | L | dateutil, icalendar |
| Evaluation harness | L | providers, judge |
