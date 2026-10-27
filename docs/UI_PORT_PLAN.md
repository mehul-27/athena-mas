# UI port plan: old Athena frontend → `athena/api/static`

Old app read-only at `M:\Athena\athena\frontend`. Only HTML, CSS, JS and static
assets were read; its `.env`, `data/`, `documents/` and `uploads/` were not
opened.

## 1. What the old frontend is

| File | Bytes | Role |
|---|---|---|
| `index.html` | 18,054 | appbar, sidebar (brand + chat list), message area, composer, three modals |
| `styles.css` | 76,145 | all design tokens and components |
| `app.js` | 42,758 | chat, research, uploads wiring |
| `settings.js` | 37,829 | provider/key screens, presets |
| `theme.js` | 24,718 | theme + accent handling |
| `report.js` | 18,872 | visual report viewer |
| `research.js` | 14,464 | deep-research controls |
| `calendar.js` | 31,121 | month/week calendar |
| `conversations.js` | 11,579 | chat list |
| `chatHeader.js` | 8,710 | header + mode switch |
| `tools.js` | 8,189 | MCP manager |
| `models.js` | 5,406 | model picker |
| `judge.js` | 5,645 | evaluation judge view |
| `report-view.html` | 2,416 | standalone report page |
| `athenalogo.svg` | 123,184 | brand mark |
| `favicon.svg` | 194 | icon |

**No remote assets**: no `@font-face`, no `url(...)`, no `https://` in the CSS
or HTML. The "Fira Code" family is resolved from locally installed fonts with
Consolas/Courier fallbacks, so nothing needs vendoring.

## 2. Design tokens (copied verbatim)

```css
--bg: #282c34;      --panel: #111418;   --panel-2: #1a1f26;  --card: #151a20;
--border: #2c4750;  --border-strong: #355a66;
--fg: #9cdef2;      --fg-dim: #7fa6b6;  --muted: #6b8a94;
--accent: #00aaff;  --accent-2: #66c7ff;
--red: #e06c75;     --green: #50fa7b;   --warn: #f0ad4e;
--font: "Fira Code", "JetBrains Mono", "Cascadia Code", Consolas, monospace;
--radius: 9px;
```

Identity: dark slate canvas, near-black panels, teal borders, cyan text,
electric-blue accent, **monospace everywhere**, compact 13.5px/1.55 body,
9px radii, thin borders instead of shadows.

The old app had **one theme** (no light variant, no toggle). This port keeps
that look as the default and adds a light theme built from the same token names
(required by the brief), plus a **Presentation size** toggle that raises the base
font to 19px and strengthens contrast for a projector.

## 3. Layout and components

Old: `appbar` (sidebar toggle, title, actions) → `sidebar` (brand, "Chats" list)
→ `main` (message stream, composer pinned at the bottom) → modals (settings,
theme, calendar, deep research).

Ported layout:

- **appbar** — logo + wordmark, status pill (crew server · mode · model · host),
  theme toggle, presentation toggle, sidebar toggle.
- **sidebar** — two groups: **History** (`GET /api/runs`) and **Demo
  recordings** (`GET /api/replay`); below them the views *How it works* and
  *Settings*. Collapsible, as before.
- **main** — one view at a time: Run, How it works, Settings, Documents.
- **composer** — question box + pattern chips (sequential / parallel /
  hierarchical / p2p), Run, Compare all four, replay speed. Enter submits,
  Shift+Enter newlines.

Component mapping (old → new):

| Old component | New home |
|---|---|
| Chat message bubble | **Result card**: answer, key points, citation chips → Sources list, confidence bar, critic verdict, tools used, Copy Markdown |
| "Thinking / steps" disclosure | **Agent activity** panel: collapsible, open while running; rows per agent (framework badge, status, ms, tokens), A2A byte lines, MCP tool calls, blackboard cards with raw-JSON toggle |
| Conversation list | History group (runs) + Demo recordings group (bundles) |
| Mode switcher (chat / deep research) | Pattern chips |
| Settings screens | Settings view: Providers (list, add/edit, Test connection, Activate, Delete), About me, Search, About/Help |
| MCP manager | merged into **How it works** as the read-only Tools list |
| Uploads | **Documents** view, kept as "coming soon" in the same style |
| Calendar, judge, models picker | **dropped** — no backend feature in this repo |

## 4. Architecture diagram

One hand-written SVG component, no library, viewBox-scaled, coloured by the same
CSS variables:

- full size in **How it works**, with a legend and per-zone captions;
- a compact **live map** inside the activity panel.

Lanes: *Google ADK* (supervisor, planner, researchers) · *CrewAI* (analyst,
writer, critic, "A2A :9001") · *MCP tools* (RAG, Web, Calculator) · the
**Blackboard** in the centre drawn as a distinct document shape. Edge labels:
`A2A`, `MCP`, `read/write`. Pattern variants: sequential, parallel, hierarchical
(dashed revision loop), p2p (chain, blackboard greyed as "audit only").

Live: nodes `idle/running/done/failed`, edges pulse on A2A/MCP/blackboard
events, A2A edges show the latest byte counts, tool nodes count calls. Reduced
motion → colours only.

`diagram.js` builds the SVG from a node/edge description per pattern;
`logic.js` maps `(events, pattern) → {nodeStates, edgeActivity, counters}` with
node tests.

## 5. Copied / adapted / dropped

- **Copied**: tokens, monospace type scale, sidebar + appbar layout, composer
  placement, button/input/chip styling, radii and border treatment, brand mark
  (`athenalogo.svg`, same licence as the old repo — MIT, this project's own).
- **Adapted**: message bubbles → result cards; chat list → history + demos;
  mode switcher → pattern chips; settings modals → settings view; theme code →
  a light/dark toggle plus presentation size.
- **Dropped**: calendar, judge, model picker, uploads UI, report viewer shell
  (the report is rendered inside the result card).

No new dependencies, no CDN, no build step.
