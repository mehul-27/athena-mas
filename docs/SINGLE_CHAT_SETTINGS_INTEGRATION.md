# Athena MAS — Single Chat, Settings, Capillary, Circum, Onboarding

Implementation note for GitHub Issue #1. Target: `athena-mas`. Reference: old `athena` (read-only).

## Baseline (Phase A)

Recorded on branch `feature/single-chat-settings`, before any implementation change.

| Command | Result |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest -q` | **506 passed, 1 skipped, 2 xfailed, 4 deselected, 4 errors** in 82.65s |
| `node athena/api/static/logic.test.js` | **26 passed, 0 failed** (exit 0) |
| `node athena/api/static/app.composer.test.js` | **6 passed, 0 failed** (exit 0) |

### Pre-existing failures (not caused by this work)

`tests/test_tools_rag.py` — all four tests error during setup with:

```text
chromadb.errors.InternalError: error returned from database: (code: 14)
unable to open database file
```

This happens inside `chromadb.PersistentClient(path=...)` for the per-test
`CHROMA_PATH` under `.test-tmp/`. It is an environment/chromadb issue present
before this task. The new Library status endpoint is written to catch this and
report `chunks: 0` with an error string instead of failing the API.

All other tests passed at baseline. New failures introduced during
implementation are tracked separately in the final report.

## Audit summary

### Old Athena Settings

- **Add Models**: 18 presets, name, base URL, API key, model + discovery datalist, key-required checkbox, Discover models, Test connection, Add provider.
- **Added Models**: provider cards, priority order, status, active tag, Test/Edit/Enable/Remove, masked key + fingerprint.
- **AI Defaults**: per-mode chat/research models, FAST/STRONG research roles.
- **Library**: saved documents + research history.
- **MCP Servers**: add/edit/remove/reconnect arbitrary stdio/SSE/HTTP servers, per-tool toggles.
- Disabled stubs: Search, Integrations/Email, Appearance, Shortcuts, Account/Admin.
- Modal mechanics: fixed backdrop, dialog, internal nav, `Esc`, outside-click, scroll lock, find filter, responsive two-column forms.

### Old Athena Single Chat

Server-persisted conversations/messages/usage; normal chat is one LLM call;
RAG and web search are explicit capabilities; tools are a separate bounded
OpenAI function-calling loop with a server-side allowlist; sources and tool
events are persisted with the assistant message; stop/compact/PDF/save menu.
Old chat was non-streaming.

### Athena MAS

Research (`/api/runs`, Blackboard, ADK + CrewAI/A2A, SSE) is production and
must stay untouched. Provider profiles + Fernet vault + activation exist.
Settings are an inline view, not a modal. There is no chat path, no plain
LiteLLM completion helper, no tool registry, no MCP management UI, and no RAG
status endpoint. `data/presets.json` is gitignored, so a fresh clone returns no
presets.

## Mapping (old → target)

| Old | Decision | Target |
|---|---|---|
| Add Models | adapt | Settings → Add model: presets, name, base, model, write-only key, reasoning, search provider, discovery, save & test |
| Added Models | adapt | Settings → Models: profile cards, active tag, test/edit/activate/delete. No priority chain or enable flag (MAS has neither) |
| AI Defaults | adapt | Real `chat_profile_id` default; research model shown read-only as the active profile; FAST/STRONG marked not applicable |
| Library | adapt | Real RAG index status + rebuild + recent research runs; saved chat documents explicitly not ported |
| MCP Servers | read-only | Built-in `athena-rag` / `athena-web` / `athena-calc` status and tools; arbitrary server management explicitly unsupported |
| Search | superseded | Already a per-profile setting; surfaced in Add/Edit model |
| Appearance | port | Theme, presentation size, and a real Capillary motion toggle |
| Normal chat | rebuild | `/api/chat`, direct LiteLLM, separate `data/chat.db` |
| RAG / web | adapt | Opt-in capabilities using the real MAS tools |
| Tools | rebuild loop, reuse tools | Server allowlist, OpenAI schemas, bounded rounds |
| Streaming / stop | new | SSE deltas + tool events + usage + terminal `done`; AbortController + server cancel |
| Compact/PDF/Save | not ported | No document store / no PDF pipeline in MAS |

## Why chat does not use ADK or CrewAI

- ADK is genuinely integrated for research (`LlmAgent`, `MCPToolset`, `LiteLlm`,
  `InMemoryRunner`), but building a per-message agent and MCP subprocesses plus
  translating ADK's event stream is more machinery than an interactive turn
  needs. It would also make cancellation and tool-selection policy harder to
  keep deterministic.
- CrewAI/A2A is unsuitable: it requires a research `run_id` and Blackboard task,
  is schema-bound to `Analysis`/`Critique`/`FinalReport`, serialises requests,
  does not stream, has no cancellation, and writes research artifacts.
- Chat uses `litellm.acompletion(stream=True)` directly, reusing the endpoint
  guard, token limiters, usage parsing, redaction, profiles and the vault.
  This is the issue's "direct LLM with bounded tool calling" path.

## Capillary source

- Official package: `asciify-engine` 4.32.8, MIT.
- Capillary is an official `asciify-engine/kit` preset:
  `import { background, capillary } from 'asciify-engine/kit'`.
- The Asciify skill states the site no longer offers downloadable template
  files; the official Capillary docs recommend the kit export. There is no
  standalone `capillary.js` helper. The official package files are vendored
  locally instead of inventing an API.
- Exact requested settings are mapped in `athena/api/static/background.js`.
  Values with no public equivalent (`bayer4`, and the inert Capillary fields
  `text`, `dither`, `look.scene`) are documented in that file and in the final
  report rather than faked.
- One persistent host (`.main`) behind Chat and Research; dynamic import after
  first paint; failure leaves the app usable; Settings pauses it; reduced motion
  and hidden-tab behavior come from the engine.

## Circum icons

- Source: `M:\Athena\Circum_1.0-Free\SVG` (24×24 single-colour SVGs).
- The supplied folder has no license/readme. The official Circum Icons
  repository is MPL-2.0; the license and attribution are included with the
  generated sprite.
- `scripts/build_circum_icons.py` selects verified files, converts the hardcoded
  fill to `currentColor`, and writes one local sprite. No remote icon CDN.

## Onboarding

- 8 steps pointing at real controls; Next/Back/Skip/Finish; progress.
- `localStorage["athena.tutorial.v1"]`, auto-start only on a first-run idle load,
  restart from the appbar help button and Settings → About.
- Focus trap/restore, Escape, Arrow keys, mobile bottom sheet, missing-target
  fallback, and a pause of the Settings focus trap while the tour targets inside
  the modal.

## Implemented

### Backend

- `athena/chat/store.py`: SQLite `data/chat.db` with conversations and messages.
  A `meta` JSON column carries tool notes, grounding notes and rejected tools.
- `athena/chat/target.py`: explicit profile → conversation profile → AI Defaults
  → active profile → `.env` resolution. A local profile may have no key; other
  profiles require a stored one.
- `athena/chat/llm.py`: direct `litellm.acompletion(stream=True)` with the
  endpoint guard scoped to the selected profile, the shared token limiters and
  provider-reported usage. Local profiles receive a non-empty placeholder
  credential because the OpenAI SDK rejects an empty one.
- `athena/chat/tools.py`: the five real MAS tools, OpenAI schemas, server-side
  allowlisting, argument clamps, bounded execution and redacted errors.
- `athena/chat/grounding.py`: opt-in RAG and web search, score floor, numbered
  sources, honest refusals.
- `athena/chat/service.py`: one turn = history → grounding → prompt → bounded
  tool loop → finalisation, with a stop event and terminal error/done events.
- `athena/api/chat_routes.py`: the `/api/chat/*` contract, SSE frames, heartbeat,
  one active stream per conversation, stop.
- `athena/api/library_routes.py`: real RAG status/count and a single-flight index
  rebuild; `athena/tools/rag/status.py` reads Chroma without loading embeddings.
- `athena/api/guards.py`: one Host/Origin policy for settings, chat and library.
- `athena/settings/defaults.py`: the real chat default profile.
- `athena/config/endpoint_guard.py`: scoped expected host for profile-specific
  chat calls, with no change for research when no scope is set.
- `athena/settings/profiles.py`: preset fallback, supported search providers
  (`duckduckgo`, `searxng`), and the dead Brave/Tavily secret path removed.

### Frontend

- `chat.js`: Chat view, conversation list, streaming/stop, model/profile picker,
  capability toggles, tool picker, sources, usage, tool activity.
- `settings.js`: the floating modal, focus trap/restore, nav filter, provider
  CRUD/test/activate/delete, discovery, defaults, appearance, library, MCP,
  personalisation and About/tour restart.
- `background.js`: one Capillary renderer singleton, dynamic import after first
  paint, view-clear updates, modal pause/play, motion toggle, failure fallback.
- `tutorial.js`: 8-step walkthrough with Next/Back/Skip/Finish, persistence,
  restart, keyboard/focus handling, missing-target fallback, mobile bottom sheet.
- `icons.js` + `assets/circum/`: local sprite from the supplied pack.
- `app.js`: shell/view dispatch, `window.AthenaUi`, settings handoff, mobile
  sidebar collapse, Capillary view/theme events.
- `style.css`: chat, modal, tutorial, icons, Capillary host and responsive rules.

## UI refinement (post-implementation)

### Capillary pointer interaction — root cause and fix

The official `background()` helper mounts the preset with `{host}`. Capillary is
a `simPreset`, and its simulation attaches `pointermove`, `pointerdown`,
`pointerleave` and `pointercancel` listeners **to that host**. The first
implementation passed the decorative `#capillary-host` div, which was covered by
`.main` and explicitly `pointer-events:none`, so the simulation never received a
pointer event. The field's own window-level tracking did not drive Capillary's
vein lighting.

The fix is to use `.main` as the host: it is the interactive surface, the kit
still creates its canvas with `pointer-events:none` and `z-index:-1`, and all UI
controls sit above it. The renderer is never remounted for a view change; the
clear map is updated through the handle.

The old whole-column clear ellipse was also hiding the effect behind the chat
content. The clear map now receives tight text-line `Range.getClientRects()`
rects, cached and invalidated on view/scroll/resize/size changes and by one
content `MutationObserver`. This protects readability while leaving the veins and
lantern light visible between lines, in margins and in empty states.

Measured with the running page (canvas region around synthetic pointer moves):
Chat brightness sum `289,756 → 446,799` (+54%); Research `898,277 → 1,321,595`
(+47%). Reduced motion keeps the canvas byte-identical and ignores pointer moves.
Hiding the vendored entry with a cache-busted import produced
`data-capillary="failed"`, zero canvases, and a fully usable Chat with models.

### Palette and typography

- Palette is centralized in the existing `:root` / `html[data-theme="light"]`
  tokens. Dark: `#090909` background, `#f2f2f0` text, `#8a8a86` muted,
  `#2a2a2a` borders. Light: `#f6f6f3` / `#161616` / `#6d6d68` / `#d6d6d1`.
  `--on-accent` gives white-on-black in light and black-on-white in dark.
  Error/warning/success remain muted red/amber/green; no blue accent.
- The reference page was inspected live (computed styles, not screenshots):
  body `Geist, ui-sans-serif, system-ui, sans-serif` at 15px/1.55, H1 weight 500
  with tight tracking, technical/code `"Geist Mono", ...`.
- Athena uses Geist for interface and prose, and Geist Mono only for model
  identifiers, run IDs, token counts, paths, tables and code. The fonts are
  vendored under `assets/fonts/` with source and SHA-256 in `SOURCES.md` and the
  upstream OFL license in `LICENSE`; there is no runtime font CDN.

### Sidebar and contextual history

- The left sidebar collapses to width 0 on desktop with a persisted
  `athena.sidebar` preference and a dynamic `aria-expanded` / `aria-label` on
  the appbar toggle. The renderer handle and canvas count stay unchanged across
  the transition.
- At 680px and below the sidebar is an absolute drawer with a backdrop; the main
  conversation stays full width instead of being squeezed. Selecting navigation
  closes the drawer; the appbar toggle reopens it.
- Chat shows the Chats group; Research/details/how/documents show Research
  history and Recordings. Existing history data is only hidden, never deleted or
  duplicated, and chat selection survives switching modes.
- On desktop, a sidebar-only tutorial step reopens a collapsed sidebar before
  highlighting the nav. On mobile the drawer stays closed and the step is skipped
  safely when its target is offscreen.

### Wordmark and new-chat heading

Three directions were considered before implementation:

1. **Stipple dot-matrix** — sparse `.`/`:` letterforms. Delicate, but fragile at
   header size and noisy when enlarged.
2. **Hairline glyphs** — `/\`, `|`, `_` strokes. Technical, but cryptic and hard
   to read as ATHENA at mobile sizes.
3. **Mono editorial** (chosen) — Geist Mono lowercase with tracking and a dotted
   baseline. Legible, restrained and coherent between header and empty state.

- The header wordmark is lowercase `athena` in Geist Mono, 11px desktop / 9px
  mobile, with `.18em` tracking and a 1px dotted baseline. The logo image is
  gone; it does not compete with status or header controls.
- The empty state is `Ask athena anything` in Geist Mono, 32px desktop / 20px
  mobile. `athena` is weight 500 and bright; `Ask`/`anything` are muted; the
  heading has the same dotted baseline. The supporting sentence remains the
  existing Geist copy and is unchanged.
- Result titles, the research eyebrow, chat turn labels, statuses, tutorial
  titles and Settings copy use lowercase `athena`; labels use weight 500.
- No new dependencies, fonts, remote assets, JavaScript, animation or broad
  heading overrides were added.

### Header and scrollbars

- The header Presentation Size button and its click wiring were removed. The
  display-size preference remains available in Settings → Appearance and still
  scales `--base`; the header actions are now help/theme only, with Settings in
  the sidebar.
- Scrollbars use Firefox `scrollbar-width: thin` plus 8px Chromium/WebKit
  pseudo-elements with a transparent track and neutral thumb. Selection colors
  are neutral rather than the browser's blue.

### Refinement verification

- Wordmarks measured at 1440: header 51×16.5px, 11px Geist Mono, 1.98px
  tracking, dotted baseline; heading 340×47px, 32px Geist Mono, `athena` weight
  500, dotted baseline. At 390: header 40×14px (9px), heading 212×30px (20px).
  `document.scrollWidth` equals 1440 and 390; no horizontal overflow.
- Dark and light were both inspected: header/heading use `--fg`/`--fg-dim` and
  `--border-strong`; no hardcoded colors.
- The header has only Help and Theme actions; Settings remains the sidebar item.
- Capillary remains one canvas and interactive: a pointer sweep changed the
  sampled canvas brightness by +39% in Chat; the renderer code was not touched.
- Settings and tutorial were re-captured with the new wordmark; both still layer
  above the background and keep one canvas.
- Static UI/API tests passed: 72; Node suites: 35 and 6.

## Verification

### Commands and results

| Command | Result |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest -q` (baseline) | 506 passed, 1 skipped, 2 xfailed, 4 deselected, **4 pre-existing chromadb errors** in 82.65s |
| `.\.venv\Scripts\python.exe -m pytest -q` (final) | **563 passed**, 1 skipped, 2 xfailed, 4 deselected, **the same 4 pre-existing chromadb errors** in 67.90s |
| `node athena/api/static/logic.test.js` | **35 passed, 0 failed** |
| `node athena/api/static/app.composer.test.js` | **6 passed, 0 failed** |
| `.\.venv\Scripts\python scripts\build_circum_icons.py --check` | exit 0 (sprite current) |

The four errors remain `tests/test_tools_rag.py` Chroma setup failures
(`unable to open database file`) under the per-test `CHROMA_PATH`. They are
environment-specific, were present before this work, and are unrelated to the
new chat path. No new test failures were introduced.

### New tests

- `tests/test_chat_store.py` — lifecycle, messages, bounded history, totals.
- `tests/test_chat_api.py` — SSE meta/delta/done, persistence, no grounding when
  off, capability/tool validation, RAG sources, missing key, cross-origin, stop.
- `tests/test_chat_llm.py` — stream assembly, tool loop, provider tool rejection,
  round/call budgets, usage only when reported.
- `tests/test_chat_tools.py` — catalogue, schemas, clamps, unknown tool, failure,
  timeout.
- `tests/test_chat_grounding.py` — source numbering, score floor, refusals.
- `tests/test_settings_extras.py` — defaults, presets fallback, library, reindex
  single-flight, MCP, model discovery.
- `tests/test_assets_and_tutorial.py` — vendor import closure, Circum symbols,
  Capillary config, tutorial/chat/settings contracts.
- `tests/test_demo_ui_and_api.py` — updated ownership for settings source checks,
  new page IDs, new files in the innerHTML/no-remote checks.

### Browser verification (manual, Chrome via DevTools)

`scripts/manual_chat_server.py` starts the offline fake provider on
`127.0.0.1:18081` and the API on `127.0.0.1:8010` for these checks. It is a
development helper, not part of the application runtime.

- Normal Chat: profile selection, streaming, persisted transcript, usage line.
- Tool loop: selected `calculate` returned `= 4.0`, one tool event, final answer.
- RAG: real NASA handbook passages (scores 0.69–0.72) shown as numbered sources.
- Web search: live DuckDuckGo results shown as numbered web sources.
- Settings: opened as a modal over Chat/Research; all eight categories loaded real
  data (`chunks: 1095`, corpus file, research runs, MCP tools).
- Background toggle: Off removed the canvas (`0`), On recreated exactly one.
- Capillary failure: hiding the vendor entry set `data-capillary="failed"`, left
  zero canvases, and Chat still loaded with models and controls.
- Reduced motion: the Capillary canvas data URL was byte-identical after 1.8s.
- Light theme: `data-theme=light`, `--capillary-ink` became `#777777`, canvas
  stayed alive.
- Tutorial: step 2 target/card both visible after the placement fix; steps 3–5
  placed below/above their targets; step 7 targeted the Library nav inside the
  Settings modal; step 8 showed Finish; Finish persisted `done`.
- Layout: 1440, 1024, 768 and 390 widths checked; `document.scrollWidth` equals
  the viewport and the main scroller has no horizontal overflow. At 390 the
  sidebar auto-collapses and the chat composer fits.
- Console: no errors on a fresh load. The only verbose message is Chrome's
  autocomplete suggestion for an input; the one transient
  `ERR_CONNECTION_REFUSED` was a health poll during a deliberate server restart.

### Screenshots

| File | Verifies |
|---|---|
| `docs/screenshots/chat-desktop.png` | Final mono-editorial wordmark and empty state at 1440px, Chats-only sidebar, Capillary. |
| `docs/screenshots/chat-mobile.png` | Final wordmark and empty state at 390px with the drawer closed. |
| `docs/screenshots/wordmark-dark-1440.png` | Wordmark/heading in dark theme at 1440px. |
| `docs/screenshots/wordmark-light-1440.png` | Wordmark/heading in light theme at 1440px. |
| `docs/screenshots/wordmark-dark-390.png` | Wordmark/heading in dark theme at 390px. |
| `docs/screenshots/wordmark-light-390.png` | Wordmark/heading in light theme at 390px. |
| `docs/screenshots/settings-desktop.png` | Settings modal at 1440px with the new header wordmark behind it. |
| `docs/screenshots/settings-mobile.png` | Settings at 390px with the new wordmark and no page overflow. |
| `docs/screenshots/capillary-chat.png` | Capillary behind Chat with the final wordmark. |
| `docs/screenshots/capillary-research.png` | Capillary behind Research with the final wordmark and research-only history. |
| `docs/screenshots/tutorial-step.png` | Desktop tour with the final wordmark and real Chat nav. |

## Deviations and decisions

- **Capillary `bayer4`:** the GPU Capillary preset has no public algorithm field;
  `bayer4` belonged to the retired canvas templates. The supported dither control
  is set to `0`; `text`, `dither` and `look.scene` are passed but are inert for
  this simulation and are documented rather than presented as visual changes.
- **Light theme ink:** the requested `#f2f0ea` is exact in the dark scheme. On the
  existing light theme it is nearly invisible, so `--capillary-ink` becomes
  `#777777`. This is stated in the Appearance hint and here.
- **Capillary host:** the engine's simulation attaches pointer listeners to the
  host it receives, so the decorative `#capillary-host` div was removed and
  `.main` is the host. The canvas remains `pointer-events:none` and sits behind
  the content; this is the fix for the non-reactive background.
- **Clear map:** the whole-column clear ellipse was replaced with tight text-line
  rects so the lantern is visible between lines and in margins without reducing
  text contrast. The rects are cached and invalidated by view/scroll/resize/size
  and a single content observer.
- **Typography:** the reference page was inspected live. Geist (body/headings) and
  Geist Mono (technical values) are vendored locally with their OFL license and
  hashes; there is no runtime font provider. Ordinary content is sans-serif;
  monospace is reserved for identifiers, counts, paths, tables and code.
- **Palette:** the blue-grey/cyan tokens were replaced with neutral black/white
  tokens, including `--on-accent`, hover/selected/inset tokens and neutral
  scrollbars and selection. No second theme system was added.
- **Sidebar:** the desktop collapse uses width/opacity and persists in
  `athena.sidebar`; mobile uses an overlay drawer with a backdrop. Research history
  and Recordings are hidden in Chat; Chats is hidden in Research. No history is
  deleted or duplicated.
- **Presentation Size:** removed from the header and its click wiring; the
  display-size preference remains in Settings → Appearance and still scales the
  base font. This keeps the header compact without removing the setting.
- **No standalone Asciify helper:** the site no longer offers downloadable
  templates; the official `asciify-engine@4.32.8` kit closure is vendored with
  hashes and MIT license (see `vendor/asciify/SOURCE.md`).
- **MCP settings:** read-only by design. Adding arbitrary servers would introduce
  arbitrary command execution.
- **Search providers:** Brave/Tavily were accepted by old validation but never
  implemented by the MAS tool. They were removed from the profile UI/validation;
  the old `test_env_for_searxng...` test was updated to assert the unused secret
  is not exported.
- **Chat storage meta column:** added beyond the plan to persist tool notes,
  grounding notes and rejected tools with the message.
- **Settings ownership:** the settings logic moved from `app.js` to `settings.js`;
  the two source-contract tests now read `settings.js` with the same assertions.
- **Mobile sidebar:** the drawer overlays the main at 680px and below; the main
  keeps the full viewport. The drawer always starts closed on a narrow load and
  the backdrop/selection closes it.

## Remaining limitations

- No saved chat documents, Compact, PDF export, LLM judge, or visual report in
  Chat.
- No per-provider priority chain; MAS uses one active profile and the environment.
- No cost accounting; tokens are shown only when the provider reports them.
- No Markdown renderer; Chat answers are plain text with preserved whitespace.
- MCP is fixed to the three built-in servers.
- Capillary requires WebGL2; without it the app uses the normal surface.
- The four pre-existing Chroma test errors remain environmental.

## Tested vs inspected

Tested: all Python/node suites above, the live browser flows listed, the failure
and reduced-motion paths, pointer brightness changes in Chat and Research,
sidebar collapse/expand and the mobile drawer, Chat/Research contextual history,
the Settings modal at 1440/1024/768/390, Presentation Size removal, the neutral
palette and Geist typography, the thin scrollbars, screenshots, and the
1440/1024/768/390 overflow checks.
Inspected only: the vendor source hashes/closure (unit-tested), the engine's
internal lifecycle (relied on its public docs and observed behavior), and the
off-screen/hidden-tab pause (engine-owned; not directly measured with a visibility
override).

## Repository hygiene

No commit, push, deployment, or publication was performed. Unrelated existing
changes under `athena/agents/`, `athena/patterns/`, `tests/test_p2p.py`, the flow
docs, and `untitled-project/` were left untouched.
