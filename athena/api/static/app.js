/* Athena UI wiring. Pure logic lives in logic.js; the SVG in diagram.js.
 * All backend text is untrusted: everything is built with textContent.
 */
"use strict";

const L = window.AthenaLogic;
const D = window.AthenaDiagram;

const TIMEOUT_MS = 150000;
const MAX_RETRIES = 5;
const TOAST_MS = 2500;
const HISTORY_LIMIT = 8;
const WATCHDOG_MS = 5000;
const LOG_TIMEOUT_MS = 15000;
const END_GRACE_MS = 1500;
const STALL_MS = 60000;
const RESEARCH_TOOLS_KEY = "athena.research.tools";
const RESEARCH_SCROLL_KEY = "athena.research.scroll.v1";
const RESEARCH_TOOL_FALLBACK = [
  { id: "rag_search", label: "Local RAG", description: "Search the indexed local document corpus.", available: true, reason: "" },
  { id: "web_search", label: "Web search", description: "Search the web through the configured provider.", available: true, reason: "" },
  { id: "fetch_page", label: "Fetch page", description: "Read the contents of one public web page.", available: true, reason: "" },
  { id: "calculate", label: "Calculation", description: "Evaluate an arithmetic expression safely.", available: true, reason: "" },
  { id: "forecast", label: "Forecasting", description: "Project a numeric series with a linear trend or smoothing.", available: true, reason: "" },
];

const state = {
  view: "run", pattern: L.DEFAULT_PATTERN, runId: null, events: [],
  source: null, retries: 0, replaying: false, playback: false, bundles: [], presets: [],
  profiles: [], active: "", editing: "", rawJson: false, busy: false,
  runs: [], historyLimit: HISTORY_LIMIT, speed: 1,
  researchTools: loadResearchToolSelection(),
  researchToolCatalog: RESEARCH_TOOL_FALLBACK.slice(),
  researchToolsReady: false,
  traceExpanded: false, traceUserToggled: false,
  tab: L.tabReducer(undefined, { type: "select", tab: "answer" }),
  request: { status: "idle", message: "", rows: [] },
  health: null, healthError: false, noticeKey: "", noticeDismissed: false,
  hasReport: false, mode: null, startFailure: false,
  phase: "idle", connection: "idle", activeRun: null, summary: null,
  snapshot: null, toolReport: null,
  seenEvents: new Set(), lastSeq: 0, lastEventAt: 0, stalled: false,
  watchdog: null, endTimer: null, logChecking: false, logError: false,
  streamToken: 0, navToken: 0, loadingRunId: null, eventsError: "",
  scrollMap: loadResearchScrollMap(), restoreScroll: null, scrollTimer: null,
  previousRun: null, phaseDetail: "",
  docPreview: null,
};

function loadResearchScrollMap() {
  try {
    const raw = JSON.parse(localStorage.getItem(RESEARCH_SCROLL_KEY) || "{}");
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    const clean = {};
    Object.keys(raw).forEach((key) => {
      const value = Number(raw[key]);
      if (Number.isFinite(value) && value >= 0) clean[key] = value;
    });
    return clean;
  } catch (_) {
    return {};
  }
}

function saveResearchScrollMap() {
  try {
    localStorage.setItem(RESEARCH_SCROLL_KEY, JSON.stringify(state.scrollMap));
  } catch (_) { /* private mode */ }
}

function saveResearchScroll() {
  if (state.view !== "run" || !state.runId) return;
  const main = $("main");
  if (!main) return;
  state.scrollMap[state.runId] = Math.max(0, Math.round(main.scrollTop));
  saveResearchScrollMap();
}

function restoreResearchScroll() {
  if (state.restoreScroll == null) return;
  const top = state.restoreScroll;
  state.restoreScroll = null;
  requestAnimationFrame(() => {
    const main = $("main");
    if (!main) return;
    const max = Math.max(0, main.scrollHeight - main.clientHeight);
    main.scrollTop = Math.min(top, max);
  });
}

function loadResearchToolSelection() {
  try {
    const raw = localStorage.getItem(RESEARCH_TOOLS_KEY);
    if (raw === null) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    return parsed.filter((value) => typeof value === "string" && value);
  } catch (_) {
    return null;
  }
}

function saveResearchToolSelection() {
  try {
    localStorage.setItem(RESEARCH_TOOLS_KEY, JSON.stringify(state.researchTools));
  } catch (_) { /* private mode */ }
}

const $ = (id) => document.getElementById(id);
const text = (node, value) => { node.textContent = value == null ? "" : String(value); };

/* ---------------------------------------------------------------- transport */

async function api(path, options = {}, timeoutMs = TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal });
    let body = null;
    try { body = await response.json(); } catch (_) { body = null; }
    if (!response.ok) {
      const detail = body && body.detail ? body.detail : "HTTP " + response.status;
      const message = typeof detail === "string" ? detail : JSON.stringify(detail);
      const error = new Error(message);
      error.status = response.status;
      error.detail = message;
      throw error;
    }
    return body;
  } catch (error) {
    if (error.name === "AbortError") {
      const timeout = new Error("timeout");
      timeout.code = "timeout";
      throw timeout;
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

/* ---------------------------------------------------------------- chrome */

let toastTimer = null;
let startFailureTimer = null;
const START_FAILURE_MS = 8000;
const ACTIVE_RUN_MESSAGE = "Another research task is still running. Wait for it to finish before starting a new one.";

function toast(message) {
  const node = $("toast");
  text(node, message);
  node.classList.remove("hidden");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), TOAST_MS);
}

function banner(message, show) {
  const node = $("banner");
  text(node, message);
  node.classList.toggle("hidden", !show);
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("athena.theme", theme); } catch (_) { /* private mode */ }
  document.dispatchEvent(new CustomEvent("athena:theme", { detail: { theme: theme } }));
}

function applySize(presentation) {
  document.documentElement.dataset.size = presentation ? "presentation" : "normal";
  try { localStorage.setItem("athena.presentation", presentation ? "1" : "0"); } catch (_) { /* ignore */ }
  document.dispatchEvent(new CustomEvent("athena:size", { detail: { presentation: presentation } }));
}

function updateSidebarBackdrop() {
  const backdrop = $("sidebar-backdrop");
  if (!backdrop) return;
  const collapsed = document.body.classList.contains("sidebar-collapsed");
  backdrop.classList.toggle("hidden", collapsed || window.innerWidth > 680);
}

function setSidebar(collapsed, persist) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  const toggle = $("sidebar-toggle");
  if (toggle) {
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    toggle.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
    toggle.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
  }
  updateSidebarBackdrop();
  if (persist !== false) {
    try { localStorage.setItem("athena.sidebar", collapsed ? "collapsed" : "open"); } catch (_) { /* private mode */ }
  }
  window.setTimeout(() => window.AthenaBackground?.updateClear(), 220);
}

function showView(name) {
  if (state.view === "run" && name !== "run") saveResearchScroll();
  if (name === "settings") {
    window.AthenaSettings?.open();
    return;
  }
  state.view = name;
  ["chat", "run", "details", "how", "documents", "eval"].forEach((view) => {
    const node = $("view-" + view);
    if (node) node.classList.toggle("hidden", view !== name);
  });
  const chatMode = name === "chat";
  const chats = $("sidebar-chats");
  const research = $("sidebar-research");
  if (chats) chats.classList.toggle("hidden", !chatMode);
  if (research) research.classList.toggle("hidden", chatMode);
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.view === name);
  });
  if (name === "chat") window.AthenaChat?.activate();
  if (name === "run") { setTab("answer"); renderLiveMap(); }
  if (name === "details") {
    if (!state.runId || state.loadingRunId) { showView("run"); return; }
    if (state.tab.tab === "answer") setTab("activity");
    renderDetailsHeader($("user-question").textContent || "");
    renderLiveMap();
    renderActivityTrace();
  }
  if (name === "how") { renderHowDiagram(); loadTools(); }
  if (name === "documents") loadDocuments();
  if (name === "eval") loadEvaluation();
  window.AthenaContext?.setView(name);
  if (name === "run") window.AthenaContext?.setSurface("research", state.runId);
  if (name === "chat") {
    window.AthenaContext?.setSurface(
      "chat",
      window.AthenaChat?.currentMessageId?.() || null,
    );
  }
  document.dispatchEvent(new CustomEvent("athena:view", { detail: { view: name } }));
  try {
    const hashView = name === "details" ? "run" : name;
    history.replaceState(null, "", "#" + hashView + (state.replaying ? "&replay=" + state.runId : ""));
  } catch (_) { /* ignore */ }
}

function isActiveRunError(error) {
  return Boolean(error) && (error.status === 409 || /is still (going|running)/i.test(String(error.message || "")));
}

function markStartFailed() {
  state.startFailure = true;
  if (startFailureTimer) clearTimeout(startFailureTimer);
  startFailureTimer = setTimeout(() => {
    state.startFailure = false;
    startFailureTimer = null;
    if (state.phase === "start_failed") {
      state.phase = "idle";
      state.phaseDetail = "";
    }
    renderStatus();
  }, START_FAILURE_MS);
}

function clearStartFailure() {
  state.startFailure = false;
  if (startFailureTimer) clearTimeout(startFailureTimer);
  startFailureTimer = null;
}

function isActiveRun() {
  return Boolean(state.activeRun && state.runId);
}

function isLocked() {
  return isActiveRun() || state.busy || state.playback;
}

function updateLockedControls() {
  const locked = isLocked();
  const newChat = $("new-chat");
  newChat.disabled = locked;
  newChat.title = locked
    ? (state.playback ? "Wait for the recording to finish." : "Wait for the current research task to finish.")
    : "";
  const compare = $("compare-all");
  compare.disabled = locked;
  compare.title = locked && !state.playback
    ? "Wait for the current research task to finish." : "";

  const mark = (selector) => {
    document.querySelectorAll(selector).forEach((item) => {
      item.classList.toggle("locked", locked);
      item.setAttribute("aria-disabled", locked ? "true" : "false");
      item.title = locked
        ? (state.playback ? "Wait for the recording to finish." : "Wait for the current research task to finish.")
        : "";
    });
  };
  mark("#history-list li:not(.sidebar-empty):not(.group-label)");
  mark("#replay-list li:not(.sidebar-empty)");

  const patternSelect = $("composer-pattern");
  if (patternSelect) {
    patternSelect.disabled = locked;
    patternSelect.title = locked
      ? (state.playback ? "Wait for the recording to finish." : "Wait for the current research task to finish.")
      : "";
  }
  const patternTrigger = $("pattern-trigger");
  if (patternTrigger) {
    patternTrigger.disabled = locked;
    patternTrigger.title = locked
      ? (state.playback ? "Wait for the recording to finish." : "Wait for the current research task to finish.")
      : "";
    if (locked) closePatternMenu(false);
  }
  const toolsTrigger = $("research-tools-trigger");
  if (toolsTrigger) {
    toolsTrigger.disabled = locked;
    toolsTrigger.title = locked
      ? (state.playback ? "Wait for the recording to finish." : "Wait for the current research task to finish.")
      : "Choose the research tools for the next run";
    if (locked) closeResearchToolsMenu(false);
  }
  updateRunButton();
  document.querySelectorAll("#pattern-chips .chip").forEach((chip) => {
    chip.disabled = locked;
    chip.classList.toggle("locked", locked);
    chip.setAttribute("aria-disabled", locked ? "true" : "false");
    chip.title = locked
      ? (state.playback ? "Wait for the recording to finish." : "Wait for the current research task to finish.")
      : "";
  });
}

function connectionLabel() {
  if (state.connection === "connecting") return "connecting";
  if (state.connection === "open") return "live updates connected";
  if (state.connection === "reconnecting") return "reconnecting";
  if (state.connection === "lost") return "live updates stopped";
  if (state.connection === "closed") return "closed";
  return "idle";
}

function renderRunStatus() {
  const node = $("exchange-status");
  const textNode = $("exchange-status-text");
  const reconnect = $("reconnect-run");
  const details = $("report-details");
  let message = "";
  let tone = "";

  if (state.phase === "starting" || state.phase === "running") {
    if (state.connection === "lost") {
      message = "Live updates stopped before a confirmed completion. The run may still be running.";
      tone = "warn";
    } else if (state.connection === "reconnecting") {
      message = "Reconnecting live updates (" + state.retries + "/" + MAX_RETRIES + ")\u2026";
      tone = "warn";
    } else if (state.connection === "connecting") {
      message = "Connecting to live updates\u2026";
    } else if (state.phase === "starting") {
      message = "Starting research\u2026";
    } else {
      message = "Researching. Live agent activity is shown below.";
    }
    if (state.endTimer && state.connection !== "lost") {
      message += " The run process has finished; waiting for a completion signal.";
      tone = "warn";
    }
    if (state.stalled) {
      message += " No new agent events for over a minute; the run may still be working.";
      tone = "warn";
    }
  } else if (state.phase === "completing") {
    message = "Loading the completed run\u2026";
  } else if (state.phase === "ended_unconfirmed") {
    message = state.hasReport
      ? "The run process ended without a completion signal. A report is available below, but completion is unconfirmed."
      : "The run process ended without a completion signal.";
    tone = "warn";
  } else if (state.phase === "failed") {
    message = state.phaseDetail || "Run failed before a confirmed completion.";
    tone = "error";
  } else if (state.phase === "completed") {
    message = "Completed.";
    tone = "ok";
  } else if (state.phase === "completed_degraded") {
    message = "Completed with warnings.";
    tone = "warn";
  } else if (state.phase === "completed_no_report") {
    message = "Completed without a report.";
    tone = "warn";
  } else if (state.phase === "history") {
    message = "Past run. This is saved history, not a live execution.";
  } else if (state.phase === "replay") {
    message = "Recorded run. This is saved playback, not a live execution.";
  } else if (state.phase === "start_failed") {
    message = state.phaseDetail || "Could not start research.";
    tone = "error";
  }

  if (state.summary && state.summary.critic_status === "rejected") {
    const reason = String(state.summary.critique_reason || "").trim();
    message += " Critic rejected"
      + (state.summary.severity ? " (" + state.summary.severity + ")" : "")
      + (reason ? ": " + reason.slice(0, 180) : ".");
    tone = "warn";
  } else if (state.summary && state.summary.critic_status === "failed") {
    message += " Critic failed; the report was not approved.";
    tone = "warn";
  }

  node.classList.toggle("hidden", !message);
  node.classList.toggle("is-warn", tone === "warn");
  node.classList.toggle("is-error", tone === "error");
  node.classList.toggle("is-ok", tone === "ok");
  text(textNode, message);
  reconnect.classList.toggle("hidden", !(state.connection === "lost" && isActiveRun()));
  if (details) {
    details.classList.toggle("hidden", !state.runId || Boolean(state.loadingRunId));
  }
}

function renderPreviousRun() {
  const node = $("previous-run-status");
  const textNode = $("previous-run-text");
  if (!state.previousRun || state.previousRun.runId === state.runId) {
    node.classList.add("hidden");
    text(textNode, "");
    return;
  }
  node.classList.remove("hidden");
  text(textNode, state.previousRun.message);
}

function activityStateText() {
  const active = ["starting", "running", "completing"].includes(state.phase);
  if (active) {
    return state.connection === "lost" ? "Reconnecting" : "Researching";
  }
  const outcome = state.summary && state.summary.outcome;
  if (state.phase === "replay") return "Recording";
  if (state.phase === "failed") return "Failed";
  if (state.phase === "ended_unconfirmed") return "Ended without confirmation";
  if (state.phase === "completed_degraded") return "Completed with warnings";
  if (state.phase === "completed_no_report") return "Completed without a report";
  if (state.phase === "completed") return "Completed";
  if (outcome === "approved") return "Completed";
  if (outcome === "rejected") return "Completed \u00b7 rejected";
  if (outcome === "failed") return "Failed";
  if (outcome === "incomplete") return "Completed with limitations";
  if (state.phase === "history") return "Recorded run";
  return "Activity";
}

function activityRow(row) {
  const item = document.createElement("li");
  item.className = "activity-row is-" + row.status;
  const dot = document.createElement("span");
  dot.className = "activity-row-dot";
  dot.setAttribute("aria-hidden", "true");
  item.appendChild(dot);

  const body = document.createElement("div");
  body.className = "activity-row-body";
  const head = document.createElement("div");
  head.className = "activity-row-head";
  const agent = document.createElement("span");
  agent.className = "activity-agent";
  text(agent, L.agentLabel(row.agent));
  const label = document.createElement("strong");
  label.className = "activity-label";
  text(label, row.label || "activity");
  head.appendChild(agent);
  head.appendChild(label);
  if (row.framework && row.framework !== "adk") {
    const badge = document.createElement("span");
    badge.className = "activity-badge";
    text(badge, row.framework);
    head.appendChild(badge);
  }
  if (row.duration != null) {
    const time = document.createElement("span");
    time.className = "activity-row-time";
    text(time, L.duration(row.duration));
    head.appendChild(time);
  }
  body.appendChild(head);
  if (row.detail && row.detail !== row.label) {
    const detail = document.createElement("p");
    detail.className = "activity-detail";
    text(detail, String(row.detail).slice(0, 240));
    body.appendChild(detail);
  }
  if (row.error) {
    const error = document.createElement("p");
    error.className = "activity-error";
    text(error, String(row.error).slice(0, 240));
    body.appendChild(error);
  }
  item.appendChild(body);
  return item;
}

function renderActivityTrace() {
  const host = $("activity-trace");
  if (!host) return;
  if (!state.runId || state.loadingRunId) {
    host.classList.add("hidden");
    const list = $("activity-list");
    if (list) list.replaceChildren();
    return;
  }
  host.classList.remove("hidden");
  const active = ["starting", "running", "completing"].includes(state.phase);
  const finished = isTerminalPhase() || state.phase === "history" || state.phase === "replay";
  const trace = L.researchTrace(state.events, {
    finished,
    summary: state.summary || null,
  });
  if (state.eventsError) {
    trace.rows.unshift({
      id: "events-unavailable",
      agent: "athena",
      label: "Activity unavailable",
      detail: state.eventsError,
      status: "failed",
      duration: null,
      framework: "ui",
      ts: "",
      error: "",
    });
    trace.failed = true;
    trace.current = trace.rows[0];
  }
  const running = (trace.running || active) && !finished;
  host.classList.toggle("is-live", running);
  host.classList.toggle("is-complete", !running);
  host.classList.toggle("is-failed", trace.failed);

  const stateNode = $("activity-state");
  if (stateNode) text(stateNode, activityStateText());
  const current = trace.current;
  const currentNode = $("activity-current");
  if (currentNode) {
    text(currentNode, current
      ? L.agentLabel(current.agent) + " \u00b7 " + (current.label || "activity")
      : trace.summary);
  }
  const timeNode = $("activity-time");
  if (timeNode) text(timeNode, trace.elapsed_ms ? L.duration(trace.elapsed_ms) : "");
  const dot = $("activity-dot");
  if (dot) {
    dot.className = "activity-dot"
      + (trace.failed ? " is-error" : running ? " is-live" : " is-done");
  }

  const list = $("activity-list");
  if (list) {
    list.replaceChildren();
    trace.rows.forEach((row) => list.appendChild(activityRow(row)));
  }
  const expanded = state.traceExpanded;
  if (list) list.classList.toggle("hidden", !expanded);
  const toggle = $("activity-toggle");
  if (toggle) toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  const expand = $("activity-expand");
  if (expand) text(expand, expanded ? "Collapse" : "Expand");
}

function toggleActivityTrace() {
  state.traceExpanded = !state.traceExpanded;
  state.traceUserToggled = true;
  renderActivityTrace();
}

function renderStatus() {
  const health = state.health;
  const replayMode = state.mode === "replay";
  const pill = L.statusPill(health || {}, replayMode);
  let label = "checking";
  let dot = "";

  if (state.phase === "starting" || state.phase === "running" || state.phase === "completing") {
    if (state.connection === "lost") { label = "Live updates stopped"; dot = "warn"; }
    else { label = "Researching"; dot = "up"; }
  }
  else if (state.busy) { label = "Researching"; dot = "up"; }
  else if (!health) { label = state.healthError ? "Offline" : "checking"; dot = state.healthError ? "down" : ""; }
  else if (!health.provider_configured) { label = "Replay only"; }
  else if (health.crew_server !== "up") { label = "Crew offline"; dot = "down"; }
  else if (state.phase === "start_failed" || state.startFailure) { label = "Start failed"; dot = "warn"; }
  else if (state.phase === "ended_unconfirmed") { label = "Run ended"; dot = "warn"; }
  else if (state.phase === "failed") { label = "Run failed"; dot = "warn"; }
  else if (state.phase === "replay") { label = "Replay"; }
  else if (state.phase === "history") { label = "Past run"; }
  else { label = "Ready"; dot = "up"; }

  text($("status-short"), label);
  $("status-pill").title = pill.tooltip
    + (state.mode === "history" ? " | view: past run" : "")
    + (isActiveRun() ? " | connection: " + connectionLabel() : "")
    + (state.startFailure ? " | last action: start failed" : "");
  text($("status-crew"), "crew: " + pill.crew);
  text($("status-mode"), "mode: " + (state.mode === "history" ? "past" : pill.mode));
  text($("status-model"), "model: " + pill.model);
  text($("status-host"), "provider: " + pill.host);
  $("status-crew-dot").className = dot ? "dot " + dot : "dot";
  renderRunStatus();
  renderPreviousRun();
  const questionNode = $("user-question");
  if (state.runId) renderDetailsHeader(questionNode ? questionNode.textContent : "");
  renderResearchResponseMeta();
  renderActivityTrace();
  window.AthenaContext?.refresh("research", state.runId);
  updateLockedControls();
  updateNotice();
}

function setNotice(key, message) {
  if (!key) {
    state.noticeKey = "";
    state.noticeDismissed = false;
    banner("", false);
    return;
  }
  if (state.noticeKey !== key) {
    state.noticeKey = key;
    state.noticeDismissed = false;
  }
  if (state.noticeDismissed) { banner("", false); return; }
  banner(message, true);
  $("banner").title = "Click to dismiss";
}

function updateNotice() {
  if (state.busy || state.replaying || state.mode === "history" || !state.health) {
    setNotice("", "");
    return;
  }
  if (!state.health.provider_configured) {
    setNotice("no-provider", "No provider configured - live research unavailable.");
  } else if (state.health.crew_server !== "up") {
    setNotice("crew-down", "CrewAI server offline - live research unavailable.");
  } else {
    setNotice("", "");
  }
}

async function refreshHealth() {
  try {
    state.health = await api("/api/health");
    state.healthError = false;
  } catch (error) {
    state.health = null;
    state.healthError = true;
  }
  renderStatus();
}

/* ---------------------------------------------------------------- tabs */

function setTab(name) {
  state.tab = L.tabReducer(state.tab, { type: "select", tab: name });
  renderTabs();
}

function setRunning(running) {
  state.tab = { ...state.tab, running: running };
  renderTabs();
  updateLockedControls();
  renderStatus();
}

function renderTabs() {
  document.querySelectorAll("#run-tabs .tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === state.tab.tab);
  });
  ["answer", "activity", "blackboard", "metrics"].forEach((name) => {
    const panel = $("panel-" + name);
    if (panel) panel.classList.toggle("hidden", state.tab.tab !== name);
  });
  $("progress-strip").classList.toggle("hidden", !state.tab.running);
}

function updateProgress() {
  const strip = L.progressStrip(state.events);
  text($("progress-strip"), strip);
  $("progress-strip").classList.toggle("hidden", !strip || !state.tab.running);
}

/* The details view holds run panels, not the answer, so its "Answer" tab is a
 * navigation control: it returns to the conversation, selects the answer
 * panel and puts focus back on that tab. It never renders a panel of its own,
 * which is why it used to look like it did nothing. */
function showAnswerTab() {
  if (state.view !== "run") showView("run");
  setTab("answer");
  const panel = $("panel-answer");
  if (panel && typeof panel.scrollIntoView === "function") {
    panel.scrollIntoView({ block: "nearest" });
  }
  const tab = $("tab-answer");
  if (tab && typeof tab.focus === "function") tab.focus({ preventScroll: true });
}

/* ---------------------------------------------------------------- lists */

function sidebarItem(label, sub, onClick) {
  const item = document.createElement("li");
  text(item, label);
  if (sub) {
    const small = document.createElement("span");
    small.className = "row-sub";
    text(small, sub);
    item.appendChild(small);
  }
  item.addEventListener("click", onClick);
  return item;
}

function emptyItem(host, message) {
  const empty = document.createElement("li");
  empty.className = "sidebar-empty";
  text(empty, message);
  host.appendChild(empty);
}

const EXAMPLE_PANEL_MAX = 440;
let examplePositionFrame = 0;

function exampleOptions() {
  return Array.from(document.querySelectorAll("#examples-list .example-item"));
}

function closeExamples(focusToggle = false) {
  const panel = $("examples-popover");
  const toggle = $("examples-toggle");
  if (!panel) return;
  panel.classList.add("hidden");
  if (toggle) {
    toggle.setAttribute("aria-expanded", "false");
    if (focusToggle) toggle.focus();
  }
}

function positionExamples() {
  const panel = $("examples-popover");
  const toggle = $("examples-toggle");
  if (!panel || !toggle || panel.classList.contains("hidden")) return;
  const margin = 12;
  const gap = 8;
  const trigger = toggle.getBoundingClientRect();
  const appbar = document.querySelector(".appbar");
  const banner = $("banner");
  const appbarBottom = appbar ? appbar.getBoundingClientRect().bottom : 0;
  const bannerBottom = banner && !banner.classList.contains("hidden")
    ? banner.getBoundingClientRect().bottom : appbarBottom;
  const topBoundary = Math.max(margin, appbarBottom + 8, bannerBottom + 4);
  const width = Math.min(EXAMPLE_PANEL_MAX, window.innerWidth - margin * 2);
  panel.style.width = width + "px";
  panel.style.maxHeight = "none";
  const natural = panel.getBoundingClientRect().height;
  const spaceAbove = trigger.top - topBoundary - gap;
  const spaceBelow = window.innerHeight - trigger.bottom - margin - gap;
  const preferredAbove = spaceAbove >= natural || spaceAbove >= spaceBelow;
  const available = Math.max(100, Math.min(EXAMPLE_PANEL_MAX, preferredAbove ? spaceAbove : spaceBelow));
  panel.style.maxHeight = available + "px";
  const rect = panel.getBoundingClientRect();
  const top = preferredAbove ? trigger.top - rect.height - gap : trigger.bottom + gap;
  const left = Math.min(
    Math.max(margin, trigger.right - rect.width),
    window.innerWidth - margin - rect.width,
  );
  panel.style.left = left + "px";
  panel.style.top = Math.max(topBoundary, Math.min(top, window.innerHeight - margin - rect.height)) + "px";
}

function scheduleExamplePosition() {
  const panel = $("examples-popover");
  if (!panel || panel.classList.contains("hidden")) return;
  cancelAnimationFrame(examplePositionFrame);
  examplePositionFrame = requestAnimationFrame(positionExamples);
}

function updateExampleSelection() {
  const question = $("question") ? $("question").value : "";
  exampleOptions().forEach((option) => {
    option.setAttribute("aria-selected", option.dataset.question === question ? "true" : "false");
  });
}

function openExamples() {
  const panel = $("examples-popover");
  const toggle = $("examples-toggle");
  if (!panel || !toggle || !exampleOptions().length) return;
  closePatternMenu(false);
  panel.classList.remove("hidden");
  toggle.setAttribute("aria-expanded", "true");
  updateExampleSelection();
  positionExamples();
  const selected = exampleOptions().find((option) => option.getAttribute("aria-selected") === "true");
  (selected || exampleOptions()[0]).focus({ preventScroll: true });
}

function handleExamplesKey(event) {
  const options = exampleOptions();
  const index = options.indexOf(document.activeElement);
  if (event.key === "Escape") {
    event.preventDefault();
    closeExamples(true);
    return;
  }
  if (event.key === "Tab") {
    closeExamples(true);
    return;
  }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    if (!options.length) return;
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    const next = index < 0
      ? (step > 0 ? 0 : options.length - 1)
      : (index + step + options.length) % options.length;
    options[next].focus();
    return;
  }
  if (event.key === "Home" || event.key === "End") {
    if (!options.length) return;
    event.preventDefault();
    options[event.key === "Home" ? 0 : options.length - 1].focus();
    return;
  }
  if (event.key === "Enter" || event.key === " ") {
    if (index < 0) return;
    event.preventDefault();
    options[index].click();
  }
}

function chooseQuestion(value) {
  const input = $("question");
  if (!input) return;
  input.value = String(value || "");
  autoGrow();
  closeExamples(false);
  input.focus();
}

async function loadQuestions() {
  const data = await api("/api/questions");
  const list = $("examples-list");
  const count = $("examples-count");
  if (!list) return;
  list.replaceChildren();
  const items = (data.demo || []).concat((data.eval || []).slice(0, 12));
  if (count) text(count, items.length + (items.length === 1 ? " question" : " questions"));
  items.forEach((item, index) => {
    const row = document.createElement("li");
    row.className = "example-item";
    row.id = "example-option-" + index;
    row.dataset.question = String(item.text);
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", "false");
    row.tabIndex = -1;
    const id = document.createElement("span");
    id.className = "example-id";
    text(id, item.id ? String(item.id) : "");
    const question = document.createElement("span");
    question.className = "example-text";
    text(question, String(item.text));
    row.appendChild(id);
    row.appendChild(question);
    row.addEventListener("click", () => chooseQuestion(item.text));
    list.appendChild(row);
  });

  const suggestions = $("empty-suggestions");
  if (suggestions) {
    suggestions.replaceChildren();
    (data.demo || []).slice(0, 3).forEach((item) => {
      if (!item.text) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "suggestion";
      text(button, String(item.text));
      button.addEventListener("click", () => chooseQuestion(item.text));
      suggestions.appendChild(button);
    });
  }
}

async function loadReplays() {
  const host = $("replay-list");
  try {
    const data = await api("/api/replay");
    state.bundles = data.bundles || [];
    host.replaceChildren();
    if (!state.bundles.length) emptyItem(host, "no recordings");
    state.bundles.forEach((bundle) => {
      host.appendChild(sidebarItem(bundle.name, bundle.pattern, () => openReplay(bundle.name)));
    });
    updateLockedControls();
  } catch (error) {
    host.replaceChildren();
    emptyItem(host, "recordings unavailable");
    updateLockedControls();
  }
}

function historyRow(row) {
  const item = document.createElement("li");
  const top = document.createElement("span");
  top.className = "row-top";

  const pattern = document.createElement("span");
  pattern.className = "badge-pattern";
  text(pattern, row.pattern);
  top.appendChild(pattern);

  const dot = document.createElement("span");
  dot.className = "verdict-dot " + (row.approved === true ? "ok" : row.approved === false ? "bad" : "");
  dot.title = L.verdictLabel(row.approved, row.severity);
  top.appendChild(dot);

  const when = document.createElement("span");
  text(when, row.when);
  top.appendChild(when);

  if (row.count > 1) {
    const count = document.createElement("span");
    count.className = "count-pill";
    text(count, "x" + row.count);
    top.appendChild(count);
  }
  item.appendChild(top);

  const question = document.createElement("span");
  question.className = "row-sub";
  text(question, row.question || "(no question)");
  item.appendChild(question);
  item.addEventListener("click", () => openRun(row.run_id));
  return item;
}

async function loadHistory() {
  const host = $("history-list");
  try {
    const data = await api("/api/runs", { cache: "no-store" });
    if (!data || !Array.isArray(data.runs)) throw new Error("unexpected history response");
    state.runs = data.runs;
    renderHistory();
  } catch (error) {
    host.replaceChildren();
    emptyItem(host, "history unavailable");
  }
}

function renderHistory() {
  const host = $("history-list");
  host.replaceChildren();
  if (!state.runs.length) {
    emptyItem(host, "no runs yet");
    updateLockedControls();
    return;
  }
  const grouped = L.groupHistory(state.runs, { limit: state.historyLimit });
  grouped.groups.forEach((group) => {
    const label = document.createElement("li");
    label.className = "group-label";
    text(label, group.label);
    host.appendChild(label);
    group.items.forEach((row) => host.appendChild(historyRow(row)));
  });
  const more = $("history-more");
  more.classList.toggle("hidden", grouped.hidden === 0);
  text(more, "show " + grouped.hidden + " more");
  updateLockedControls();
}

/* ---------------------------------------------------------------- run */

function setPattern(value, render = true) {
  const next = L.patternValue(value);
  state.pattern = next;
  const select = $("composer-pattern");
  if (select) select.value = next;
  syncPatternPicker(next);
  document.querySelectorAll("#pattern-chips .chip").forEach((chip) => {
    chip.classList.toggle("selected", chip.dataset.pattern === next);
  });
  if (render) renderLiveMap();
}

function syncPatternPicker(next) {
  const current = $("pattern-current");
  if (current) text(current, next);
  document.querySelectorAll("#pattern-menu .pattern-option").forEach((option) => {
    const selected = option.dataset.pattern === next;
    option.classList.toggle("selected", selected);
    option.setAttribute("aria-selected", selected ? "true" : "false");
  });
}

function patternMenuOptions() {
  return Array.from(document.querySelectorAll("#pattern-menu .pattern-option"));
}

function closePatternMenu(focusTrigger) {
  const menu = $("pattern-menu");
  if (!menu) return;
  menu.classList.add("hidden");
  const trigger = $("pattern-trigger");
  if (trigger) trigger.setAttribute("aria-expanded", "false");
  if (focusTrigger && trigger) trigger.focus();
}

function openPatternMenu() {
  const menu = $("pattern-menu");
  const trigger = $("pattern-trigger");
  if (!menu || !trigger || trigger.disabled) return;
  closeExamples(false);
  menu.classList.remove("hidden");
  trigger.setAttribute("aria-expanded", "true");
  const options = patternMenuOptions();
  const selected = options.find((option) => option.getAttribute("aria-selected") === "true");
  (selected || options[0])?.focus();
}

function choosePatternOption(option) {
  if (!option) return;
  setPattern(option.dataset.pattern);
  closePatternMenu(true);
}

/* ------------------------------------------------------- research tools */

function researchToolIds() {
  return state.researchToolCatalog.map((tool) => tool.id);
}

function normaliseResearchTools(value) {
  return L.normaliseToolSelection(value, researchToolIds());
}

function hasResearchTools() {
  return state.researchTools === null || state.researchTools.length > 0;
}

function researchToolsLabel() {
  return L.toolSelectionLabel(state.researchTools);
}

function toolMenuOptions() {
  return Array.from(document.querySelectorAll("#research-tools-menu .tool-option"));
}

function renderToolMenuOption(id, label, description, checked, extra) {
  const option = document.createElement("li");
  option.className = "pattern-option tool-option"
    + (checked ? " selected" : "")
    + (extra && extra.disabled ? " is-disabled" : "");
  option.dataset.tool = id;
  option.setAttribute("role", "option");
  option.setAttribute("aria-selected", checked ? "true" : "false");
  option.setAttribute("aria-disabled", extra && extra.disabled ? "true" : "false");
  option.tabIndex = -1;

  const check = document.createElement("span");
  check.className = "tool-check";
  check.setAttribute("aria-hidden", "true");
  option.appendChild(check);

  const body = document.createElement("span");
  body.className = "tool-option-body";
  const strong = document.createElement("strong");
  text(strong, label);
  body.appendChild(strong);
  if (description) {
    const desc = document.createElement("span");
    desc.className = "tool-option-desc";
    text(desc, description);
    body.appendChild(desc);
  }
  if (extra && extra.reason) {
    const reason = document.createElement("span");
    reason.className = "tool-option-reason";
    text(reason, extra.reason);
    body.appendChild(reason);
  }
  option.appendChild(body);
  option.addEventListener("click", () => chooseResearchTool(option));
  return option;
}

function renderResearchToolMenu() {
  const host = $("research-tools-menu");
  if (!host) return;
  host.replaceChildren();
  const allSelected = state.researchTools === null;
  host.appendChild(renderToolMenuOption(
    "__all__",
    "All tools",
    "Let the planner use every enabled research capability.",
    allSelected,
    {},
  ));
  state.researchToolCatalog.forEach((tool) => {
    const selected = allSelected
      || (Array.isArray(state.researchTools) && state.researchTools.includes(tool.id));
    host.appendChild(renderToolMenuOption(
      tool.id,
      tool.label || tool.id,
      tool.description || "",
      selected,
      {
        disabled: tool.available === false,
        reason: tool.available === false ? (tool.reason || "Unavailable") : "",
      },
    ));
  });
}

function syncResearchTools() {
  const current = $("research-tools-current");
  if (current) text(current, researchToolsLabel());
  const trigger = $("research-tools-trigger");
  if (trigger) {
    trigger.classList.toggle("is-empty", !hasResearchTools());
    trigger.title = hasResearchTools()
      ? "Choose the research tools for the next run"
      : "Select at least one research tool before starting a run";
  }
  const note = $("research-tools-note");
  if (note) {
    text(note, hasResearchTools()
      ? "applies to the next run"
      : "select at least one tool to run");
    note.classList.toggle("is-warning", !hasResearchTools());
  }
  document.querySelectorAll("#research-tools-menu .tool-option").forEach((option) => {
    const id = option.dataset.tool;
    const selected = id === "__all__"
      ? state.researchTools === null
      : state.researchTools === null || state.researchTools.includes(id);
    option.classList.toggle("selected", selected);
    option.setAttribute("aria-selected", selected ? "true" : "false");
  });
  updateRunButton();
}

function chooseResearchTool(option) {
  if (!option || option.getAttribute("aria-disabled") === "true") return;
  const id = option.dataset.tool;
  state.researchTools = L.toggleToolSelection(state.researchTools, id, researchToolIds());
  saveResearchToolSelection();
  renderResearchToolMenu();
  syncResearchTools();
  const next = toolMenuOptions().find((item) => item.dataset.tool === id);
  if (next) next.focus({ preventScroll: true });
}

function openResearchToolsMenu() {
  const menu = $("research-tools-menu");
  const trigger = $("research-tools-trigger");
  if (!menu || !trigger || trigger.disabled) return;
  closePatternMenu(false);
  closeExamples(false);
  renderResearchToolMenu();
  menu.classList.remove("hidden");
  trigger.setAttribute("aria-expanded", "true");
  syncResearchTools();
  const selected = toolMenuOptions().find((item) => item.getAttribute("aria-selected") === "true");
  (selected || toolMenuOptions()[0])?.focus({ preventScroll: true });
}

function closeResearchToolsMenu(focusTrigger) {
  const menu = $("research-tools-menu");
  if (!menu) return;
  menu.classList.add("hidden");
  const trigger = $("research-tools-trigger");
  if (trigger) trigger.setAttribute("aria-expanded", "false");
  if (focusTrigger && trigger) trigger.focus();
}

function handleResearchToolsKey(event) {
  const options = toolMenuOptions();
  const index = options.indexOf(document.activeElement);
  if (event.key === "Escape") {
    event.preventDefault();
    closeResearchToolsMenu(true);
    return;
  }
  if (event.key === "Tab") {
    closeResearchToolsMenu(true);
    return;
  }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    if (!options.length) return;
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    const next = index < 0
      ? (step > 0 ? 0 : options.length - 1)
      : (index + step + options.length) % options.length;
    options[next].focus();
    return;
  }
  if (event.key === "Home" || event.key === "End") {
    if (!options.length) return;
    event.preventDefault();
    options[event.key === "Home" ? 0 : options.length - 1].focus();
    return;
  }
  if (event.key === "Enter" || event.key === " ") {
    if (index < 0) return;
    event.preventDefault();
    chooseResearchTool(options[index]);
  }
}

async function loadResearchTools() {
  try {
    const data = await api("/api/research/tools");
    const tools = Array.isArray(data.tools) ? data.tools : [];
    if (tools.length) state.researchToolCatalog = tools;
    state.researchToolsReady = true;
  } catch (_) {
    state.researchToolsReady = false;
  }
  state.researchTools = normaliseResearchTools(state.researchTools);
  saveResearchToolSelection();
  renderResearchToolMenu();
  syncResearchTools();
}

function updateRunButton() {
  const button = $("run-button");
  if (!button) return;
  button.disabled = Boolean(state.busy) || !hasResearchTools();
  text(button, state.busy ? "running..." : "Run");
}

function pattern() {
  const select = $("composer-pattern");
  return L.patternValue(select ? select.value : state.pattern);
}

function autoGrow() {
  const box = $("question");
  box.style.height = "auto";
  box.style.height = Math.min(box.scrollHeight, 68) + "px";
}

function setBusy(busy) {
  state.busy = busy;
  updateRunButton();
  updateLockedControls();
  renderStatus();
}

function resetReport(message) {
  state.hasReport = false;
  fillAnswer($("report-answer"), message || "", 0, () => {});
  $("report-points").replaceChildren();
  $("report-meta").replaceChildren();
  $("critique-detail").replaceChildren();
  $("critique-detail").className = "critique-detail hidden";
  $("report-citations").replaceChildren();
  const provenance = $("provenance-panel");
  if (provenance) {
    provenance.classList.add("hidden");
    const hops = $("provenance-hops");
    if (hops) hops.replaceChildren();
    const note = $("provenance-empty");
    if (note) { note.classList.add("hidden"); text(note, ""); }
  }
}

function showExchange(question) {
  $("empty-state").classList.add("hidden");
  $("exchange").classList.remove("hidden");
  text($("user-question"), question || "(no question recorded)");
  setTab("answer");
}

function showEmpty() {
  $("exchange").classList.add("hidden");
  $("empty-state").classList.remove("hidden");
  text($("user-question"), "");
  setTab("answer");
}

function lockedReason() {
  if (state.playback) return "A recording is playing. Wait for it to finish.";
  if (isActiveRun()) return ACTIVE_RUN_MESSAGE;
  if (state.busy) return "Another action is still running. Wait for it to finish.";
  return "";
}

function newChat() {
  if (window.innerWidth <= 680) setSidebar(true, false);
  if (state.view === "chat" && window.AthenaChat) {
    window.AthenaChat.newConversation();
    return;
  }
  if (isLocked()) { toast(lockedReason()); return; }
  saveResearchScroll();
  closeStream();
  stopWatchdog();
  clearEndTimer();
  state.navToken += 1;
  state.restoreScroll = null;
  state.loadingRunId = null;
  state.eventsError = "";
  state.runId = null;
  state.events = [];
  state.retries = 0;
  state.replaying = false;
  state.mode = null;
  state.hasReport = false;
  state.activeRun = null;
  state.busy = false;
  state.phase = "idle";
  state.connection = "idle";
  state.phaseDetail = "";
  state.summary = null;
  state.snapshot = null;
  state.toolReport = null;
  state.seenEvents.clear();
  state.lastSeq = 0;
  state.lastEventAt = 0;
  state.stalled = false;
  state.logChecking = false;
  state.logError = false;
  state.previousRun = null;
  state.traceExpanded = false;
  state.traceUserToggled = false;
  clearStartFailure();
  $("activity-trace").classList.add("hidden");
  $("activity-list").replaceChildren();
  renderToolReport(null, null);
  $("report-details").classList.add("hidden");
  text($("details-run-label"), "Open a Research answer to inspect its run.");
  $("replay-badge").classList.add("hidden");
  $("replay-speed-row").classList.add("hidden");
  text($("footer-run"), "run: -");
  $("link-json").href = "#";
  $("link-log").href = "#";
  $("event-log").replaceChildren();
  $("timeline-body").replaceChildren();
  $("blackboard-sections").replaceChildren();
  text($("progress-strip"), "");
  $("heatmap").removeAttribute("src");
  resetMetrics("");
  resetReport("");
  text($("report-answer"), "Ask a question, or replay a recording.");
  $("question").value = "";
  autoGrow();
  setRunning(false);
  setBusy(false);
  window.AthenaContext?.setSurface("research", null);
  showView("run");
  showEmpty();
  renderStatus();
  renderResearchResponseMeta();
  $("question").focus();
}

async function startRun() {
  const submittedQuestion = $("question").value;
  if (!submittedQuestion.trim()) { toast("Type a question first."); return; }
  if (!hasResearchTools()) {
    toast("Select at least one research tool before starting a run.");
    openResearchToolsMenu();
    return;
  }
  if (isLocked()) { toast(lockedReason()); return; }
  const priorPhase = state.phase;
  const priorDetail = state.phaseDetail;
  const selectedPattern = pattern();
  setPattern(selectedPattern, false);
  state.phase = "starting";
  state.phaseDetail = "";
  try {
    setBusy(true);
    const data = await api("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: submittedQuestion,
        pattern: state.pattern,
        tools: state.researchTools === null ? null : state.researchTools.slice(),
      }),
    });
    if (!L.validRunId(data && data.run_id)) {
      throw new Error("server did not return a valid run id");
    }
    $("question").value = "";
    autoGrow();
    showView("run");
    showExchange(submittedQuestion);
    beginRun(data.run_id, false, "live");
    resetReport("athena is researching this question\u2026");
  } catch (error) {
    const active = isActiveRunError(error);
    addLog("start", "start failed: " + error.message, "error");
    markStartFailed();
    if (!state.runId) {
      state.phase = "start_failed";
      state.phaseDetail = active ? ACTIVE_RUN_MESSAGE : "Could not start research.";
    } else {
      state.phase = priorPhase;
      state.phaseDetail = priorDetail;
    }
    setBusy(false);
    refreshHealth();
    toast(active ? ACTIVE_RUN_MESSAGE : "Could not start: " + error.message);
  }
}

function beginRun(runId, replaying, mode) {
  stopWatchdog();
  clearEndTimer();
  closeStream();
  clearStartFailure();
  state.runId = runId;
  state.loadingRunId = null;
  state.eventsError = "";
  state.replaying = replaying;
  state.mode = mode || (replaying ? "replay" : "live");
  if (state.mode === "live") state.restoreScroll = null;
  state.events = [];
  state.seenEvents = new Set();
  state.lastSeq = 0;
  state.lastEventAt = Date.now();
  state.stalled = false;
  state.summary = null;
  state.snapshot = null;
  state.toolReport = null;
  state.retries = 0;
  state.logChecking = false;
  state.logError = false;
  state.activeRun = state.mode === "live" ? { id: runId } : null;
  state.connection = state.mode === "live" ? "connecting" : "idle";
  state.phase = state.mode === "live" ? "starting" : state.mode;
  state.phaseDetail = "";
  state.traceExpanded = state.mode === "live" || state.mode === "replay";
  state.traceUserToggled = false;
  resetReport("");
  $("replay-badge").classList.toggle("hidden", !replaying);
  $("replay-speed-row").classList.toggle("hidden", !replaying);
  text($("footer-run"), "run: " + runId);
  $("link-json").href = "/api/runs/" + runId;
  $("link-log").href = "/api/runs/" + runId + "/log";
  $("event-log").replaceChildren();
  $("timeline-body").replaceChildren();
  renderToolReport(null, null);
  text($("progress-strip"), "");
  $("heatmap").removeAttribute("src");
  resetMetrics("");
  renderLiveMap();
  if (state.mode === "live") {
    setBusy(true);
    setRunning(true);
    openStream(runId);
    startWatchdog(runId);
  } else {
    setBusy(false);
    setRunning(state.mode === "replay");
  }
  window.AthenaContext?.setSurface("research", runId);
  renderStatus();
}

function isTerminalPhase() {
  return ["completed", "completed_degraded", "completed_no_report", "failed",
    "ended_unconfirmed"].includes(state.phase);
}

function handleStreamError(runId) {
  if (!isActiveRun() || state.runId !== runId || isTerminalPhase()) return;
  state.retries += 1;
  if (state.retries > MAX_RETRIES) {
    state.connection = "lost";
    state.phaseDetail = "Live updates stopped before a confirmed completion.";
    addLog("stream", "stream ended", "error");
    closeStream();
    startWatchdog(runId);
    renderStatus();
    return;
  }
  state.connection = "reconnecting";
  addLog("stream", "reconnecting (" + state.retries + "/" + MAX_RETRIES + ")");
  renderStatus();
}

function handleMalformedDone(runId) {
  if (!isActiveRun() || state.runId !== runId || isTerminalPhase()) return;
  addLog("done", "malformed completion payload; not completing", "error");
  state.connection = "lost";
  state.phaseDetail = "The completion signal was malformed.";
  closeStream();
  startWatchdog(runId);
  renderStatus();
}

function handleDone(runId, message) {
  if (!isActiveRun() || state.runId !== runId || isTerminalPhase()) return;
  let payload;
  try {
    payload = JSON.parse(message.data);
  } catch (_) {
    handleMalformedDone(runId);
    return;
  }
  if (!payload || typeof payload.summary !== "object" || payload.summary === null
      || Array.isArray(payload.summary)) {
    handleMalformedDone(runId);
    return;
  }
  completeRun(runId, payload.summary);
}

function openStream(runId) {
  closeStream();
  const token = state.streamToken + 1;
  state.streamToken = token;
  state.connection = "connecting";
  const source = new EventSource("/api/runs/" + runId + "/stream");
  state.source = source;
  const current = () => token === state.streamToken && isActiveRun() && state.runId === runId;

  source.onopen = () => {
    if (!current()) return;
    state.connection = "open";
    if (state.phase === "starting") state.phase = "running";
    renderStatus();
  };
  source.onmessage = (message) => {
    if (!current()) return;
    let event;
    try {
      event = JSON.parse(message.data);
    } catch (_) {
      addLog("stream", "ignored malformed event", "error");
      return;
    }
    addEvent(event);
  };
  source.addEventListener("blackboard", () => {
    if (current()) loadSnapshot(runId);
  });
  source.addEventListener("done", (message) => {
    if (!current()) return;
    handleDone(runId, message);
  });
  source.onerror = () => {
    if (!current()) return;
    handleStreamError(runId);
  };
  renderStatus();
}

function closeStream() {
  state.streamToken += 1;
  if (state.source) {
    const source = state.source;
    state.source = null;
    source.close();
  }
}

function addEvent(event) {
  if (!event || typeof event !== "object") return;
  if (event.run_id && state.runId && event.run_id !== state.runId) return;
  const eventId = typeof event.event_id === "string" ? event.event_id : "";
  const seq = Number(event.seq);
  if (eventId && state.seenEvents.has(eventId)) return;
  if (Number.isFinite(seq) && seq > 0 && seq <= state.lastSeq) return;
  if (eventId) state.seenEvents.add(eventId);
  if (Number.isFinite(seq) && seq > state.lastSeq) state.lastSeq = seq;

  state.events.push(event);
  state.lastEventAt = Date.now();
  const reconnected = state.connection === "connecting" || state.connection === "reconnecting";
  if (reconnected) state.connection = "open";
  if (state.phase === "starting") {
    state.phase = "running";
    renderStatus();
  } else if (reconnected) {
    renderStatus();
  }
  if (state.stalled) {
    state.stalled = false;
    renderStatus();
  }
  renderTimeline();
  renderLiveMap();
  updateProgress();
  renderActivityTrace();
  renderResearchResponseMeta();
  window.AthenaContext?.refresh("research", state.runId);
  logEvent(event);
}

function startWatchdog(runId) {
  stopWatchdog();
  state.watchdog = setInterval(() => {
    if (!isActiveRun() || state.runId !== runId || isTerminalPhase()) {
      stopWatchdog();
      return;
    }
    checkStall();
    checkProcess(runId);
  }, WATCHDOG_MS);
}

function stopWatchdog() {
  if (state.watchdog) {
    clearInterval(state.watchdog);
    state.watchdog = null;
  }
}

function clearEndTimer() {
  if (state.endTimer) {
    clearTimeout(state.endTimer);
    state.endTimer = null;
  }
}

function checkStall() {
  if (!isActiveRun() || state.stalled) return;
  if (state.phase !== "starting" && state.phase !== "running") return;
  if (Date.now() - state.lastEventAt > STALL_MS) {
    state.stalled = true;
    renderStatus();
  }
}

async function checkProcess(runId) {
  if (!isActiveRun() || state.runId !== runId || isTerminalPhase() || state.logChecking) return;
  state.logChecking = true;
  try {
    const log = await api("/api/runs/" + runId + "/log", {}, LOG_TIMEOUT_MS);
    if (!isActiveRun() || state.runId !== runId || isTerminalPhase()) return;
    state.logError = false;
    if (log && log.finished && !state.endTimer) {
      stopWatchdog();
      const exitCode = log.exit_code == null ? null : Number(log.exit_code);
      addLog("process", "run process finished; waiting for completion signal");
      state.endTimer = setTimeout(() => {
        state.endTimer = null;
        endRunUnconfirmed(runId, exitCode);
      }, END_GRACE_MS);
      renderStatus();
    }
  } catch (_) {
    if (!state.logError) {
      state.logError = true;
      addLog("process", "could not check whether the run process is still active", "error");
    }
  } finally {
    state.logChecking = false;
  }
}

async function completeRun(runId, summary) {
  if (!isActiveRun() || state.runId !== runId || isTerminalPhase()) return;
  stopWatchdog();
  clearEndTimer();
  closeStream();
  state.connection = "closed";
  state.activeRun = null;
  state.summary = summary;
  state.phase = "completing";
  state.phaseDetail = "";
  addLog("done", "degraded=" + Boolean(summary.degraded) + " "
    + L.verdictLabel(summary.approved, summary.severity));
  renderStatus();
  await loadSnapshot(runId).catch(() => {});
  const hasReport = state.hasReport;
  if (summary.failed) {
    state.phase = "failed";
    state.phaseDetail = "Run failed before a successful result.";
    state.previousRun = { runId, message: "Previous run failed." };
  } else if (!hasReport) {
    state.phase = "completed_no_report";
    state.phaseDetail = "Completed without a report.";
  } else if (summary.degraded) {
    state.phase = "completed_degraded";
    state.phaseDetail = "Completed with warnings.";
  } else {
    state.phase = "completed";
    state.phaseDetail = "Completed.";
  }
  if (!state.traceUserToggled) state.traceExpanded = false;
  setBusy(false);
  setRunning(false);
  renderStatus();
  await loadHistory();
}

async function endRunUnconfirmed(runId, exitCode) {
  if (!isActiveRun() || state.runId !== runId || isTerminalPhase()) return;
  stopWatchdog();
  clearEndTimer();
  closeStream();
  state.connection = "closed";
  state.activeRun = null;
  state.phase = "completing";
  state.phaseDetail = "The run process ended without a completion signal.";
  renderStatus();
  await loadSnapshot(runId).catch(() => {});
  const failed = exitCode != null && exitCode !== 0;
  state.phase = failed ? "failed" : "ended_unconfirmed";
  state.phaseDetail = failed
    ? "The run process exited with code " + exitCode + " before a completion signal."
    : "The run process ended without a completion signal.";
  state.previousRun = {
    runId,
    message: failed ? "Previous run failed." : "Previous run ended without a completion signal.",
  };
  if (!state.traceUserToggled) state.traceExpanded = false;
  setBusy(false);
  setRunning(false);
  renderStatus();
  await loadHistory();
}

function reconnectRun() {
  if (!isActiveRun() || state.connection !== "lost") return;
  state.retries = 0;
  state.connection = "connecting";
  state.phaseDetail = "";
  state.lastEventAt = Date.now();
  state.stalled = false;
  addLog("stream", "manual reconnect");
  setBusy(true);
  openStream(state.runId);
  startWatchdog(state.runId);
  renderStatus();
}

function addLog(kind, message, cssClass) {
  const item = document.createElement("li");
  item.className = cssClass || "";
  text(item, kind + " \u00b7 " + message);
  $("event-log").appendChild(item);
  $("event-log").scrollTop = $("event-log").scrollHeight;
}

/* Every recorded kind gets a row. The formatting is a pure function in
 * logic.js (`eventLogEntry`) so all kinds — including retry, guardrail,
 * handoff, recovery, skip, limiter_wait, model_call and stage_start — are
 * covered by node tests, and an unknown kind still shows its own name. */
function logEvent(event) {
  const row = L.eventLogEntry(event);
  if (!row) return;
  addLog(row.label, row.text || row.kind, row.tone);
}

function renderTimeline() {
  const body = $("timeline-body");
  body.replaceChildren();
  L.reduceTimeline(state.events).forEach((row) => {
    const tr = document.createElement("tr");
    tr.className = row.status;
    [row.agent, row.framework.toUpperCase(), row.status, L.duration(row.ms),
     L.tokens(row.tokens_in, row.tokens_out, row.tokens_known),
     row.stop_reason || ""].forEach((value) => {
      const cell = document.createElement("td");
      text(cell, value);
      tr.appendChild(cell);
    });
    body.appendChild(tr);
  });
}

function updateDiagramScrollHints() {
  [["live-map", "live-scroll-hint"], ["how-diagram", "how-scroll-hint"]].forEach(([hostId, hintId]) => {
    const host = $(hostId);
    const hint = $(hintId);
    if (!host || !hint) return;
    const scrollable = host.scrollWidth > host.clientWidth + 1;
    hint.classList.toggle("hidden", !scrollable);
    if (scrollable) host.setAttribute("aria-describedby", hintId);
    else host.removeAttribute("aria-describedby");
  });
}

function renderLiveMap() {
  const spec = L.diagramFor(state.pattern);
  const view = L.diagramState(state.events, state.pattern);
  D.render($("live-map"), spec, view, { compact: true });
  updateDiagramScrollHints();
}

/* ---------------------------------------------------------------- replay */

async function openReplay(name, options) {
  const opts = options || {};
  if (isLocked()) { toast(lockedReason()); return; }
  saveResearchScroll();
  const token = state.navToken + 1;
  state.navToken = token;
  state.restoreScroll = null;
  state.playback = true;
  try {
    const bundle = await api("/api/replay/" + name);
    if (token !== state.navToken) return;
    const at = typeof opts.at === "number" ? Math.max(0, Math.min(1, opts.at)) : 1;
    showView("run");
    setPattern((bundle.summary && bundle.summary.pattern) || state.pattern, false);
    beginRun(bundle.run_id, true);
    state.summary = bundle.summary || null;
    state.snapshot = bundle.blackboard
      ? { ...bundle.blackboard, evidence: bundle.evidence || [] }
      : null;
    state.toolReport = (state.snapshot && state.snapshot.tool_report) || null;
    state.restoreScroll = Number.isFinite(state.scrollMap[bundle.run_id])
      ? state.scrollMap[bundle.run_id]
      : null;
    renderResearchResponseMeta();
    try {
      history.replaceState(null, "", "#replay=" + encodeURIComponent(name) + (at < 1 ? "&at=" + at : ""));
    } catch (_) { /* ignore */ }
    showExchange(bundle.blackboard && bundle.blackboard.task && bundle.blackboard.task.value
      ? bundle.blackboard.task.value.question : "");
    resetReport("Preparing recording\u2026");
    const stopAt = Math.max(1, Math.floor(bundle.events.length * at));
    const speed = state.speed;
    const first = bundle.events.length ? Date.parse(bundle.events[0].ts) : 0;
    let previous = 0;
    for (let index = 0; index < stopAt; index += 1) {
      const event = bundle.events[index];
      if (speed > 0) {
        const when = Date.parse(event.ts) - first;
        const wait = Math.max(0, (when - previous) / speed);
        previous = when;
        await new Promise((resolve) => setTimeout(resolve, Math.min(wait, 400)));
      }
      addEvent(event);
    }
    if (bundle.blackboard) {
      renderBlackboard({ ...bundle.blackboard, evidence: bundle.evidence || [] });
      const final = bundle.blackboard.final_report;
      if (final) renderReport(final.value, bundle.blackboard.critique);
    }
    if (!state.hasReport && at >= 1) resetReport("No report is available for this recording.");
    if (bundle.heatmap_png) {
      $("heatmap").src = "data:image/png;base64," + bundle.heatmap_png;
      $("heatmap").classList.remove("hidden");
      text($("metrics-note"), "recorded replay heatmap; per-agent table is not stored in this recording");
    }
    if (at < 1) {
      addLog("replay", "paused at " + Math.round(at * 100) + "% of the recording");
      if (opts.keepRunning !== false) setRunning(true);
    } else {
      addLog("replay", "finished " + name + " (" + bundle.events.length + " events)");
      setRunning(false);
    }
    if (opts.tab && opts.tab !== "answer") {
      showView("details");
      setTab(opts.tab);
      try {
        history.replaceState(null, "", "#replay=" + encodeURIComponent(name)
          + (at < 1 ? "&at=" + at : "") + "&tab=" + opts.tab);
      } catch (_) { /* ignore */ }
    }
    if (at >= 1 && !state.traceUserToggled) state.traceExpanded = false;
    renderActivityTrace();
    renderResearchResponseMeta();
    window.AthenaContext?.refresh("research", state.runId);
    restoreResearchScroll();
    await refreshHealth();
  } catch (error) {
    toast("Replay failed: " + error.message);
  } finally {
    state.playback = false;
    renderStatus();
  }
}

async function openRun(runId) {
  if (isLocked()) { toast(lockedReason()); return; }
  saveResearchScroll();
  const token = state.navToken + 1;
  state.navToken = token;
  state.loadingRunId = runId;
  state.restoreScroll = null;
  window.AthenaContext?.setSurface("research", null);
  showView("run");
  renderStatus();
  try {
    const snapshot = await api("/api/runs/" + runId);
    if (token !== state.navToken) return;
    setPattern(
      (snapshot.task && snapshot.task.value.pattern) || state.pattern,
      false,
    );
    beginRun(runId, false, "history");
    state.summary = snapshot.summary || null;
    state.snapshot = snapshot;
    state.toolReport = snapshot.tool_report || null;
    state.restoreScroll = Number.isFinite(state.scrollMap[runId])
      ? state.scrollMap[runId]
      : null;
    try { history.replaceState(null, "", "#run"); } catch (_) { /* ignore */ }
    const question = snapshot.task && snapshot.task.value ? snapshot.task.value.question : "";
    showExchange(question);
    resetReport("Loading run\u2026");
    renderDetailsHeader(question);
    renderBlackboard(snapshot);
    renderToolReport(snapshot.tool_report, state.summary);
    renderResearchResponseMeta();
    await loadRunEvents(runId);
    if (token !== state.navToken) return;
    if (snapshot.final_report) renderReport(snapshot.final_report.value, snapshot.critique);
    if (!state.hasReport) resetReport("No report is available for this run.");
    await loadMetrics(runId);
    if (token !== state.navToken) return;
    renderLiveMap();
    renderStatus();
    setRunning(false);
    restoreResearchScroll();
  } catch (error) {
    if (token !== state.navToken) return;
    state.loadingRunId = null;
    newChat();
    toast("Could not open run: " + error.message);
  }
}

function renderDetailsHeader(question) {
  const node = $("details-run-label");
  if (!node) return;
  const pattern = (state.summary && state.summary.pattern) || state.pattern;
  const outcome = state.summary && state.summary.outcome;
  const bits = [
    state.runId ? "run " + state.runId.slice(0, 8) : "no run selected",
    pattern || "",
    outcome || "",
  ].filter(Boolean);
  text(node, bits.join(" \u00b7 ") + (question ? " \u00b7 " + question : ""));
}

async function loadRunEvents(runId) {
  let data = null;
  let failed = false;
  try {
    data = await api("/api/runs/" + encodeURIComponent(runId) + "/events");
  } catch (_) {
    failed = true;
  }
  if (state.runId !== runId) return;
  state.events = failed ? [] : (Array.isArray(data && data.events) ? data.events : []);
  state.eventsError = failed
    ? "Activity could not be loaded for this run."
    : "";
  state.seenEvents = new Set(
    state.events.map((event) => event.event_id).filter(Boolean),
  );
  state.lastSeq = state.events.reduce(
    (last, event) => Math.max(last, Number(event.seq) || 0), 0,
  );
  state.lastEventAt = Date.now();
  const log = $("event-log");
  if (log) {
    log.replaceChildren();
    state.events.forEach((event) => logEvent(event));
  }
  renderTimeline();
  renderLiveMap();
  updateProgress();
  renderActivityTrace();
  renderResearchResponseMeta();
  window.AthenaContext?.refresh("research", state.runId);
}

function renderToolReport(report, summary) {
  const host = $("tool-report");
  const note = $("tool-report-note");
  if (!host) return;
  host.replaceChildren();
  if (!report) {
    if (note) text(note, state.runId ? "waiting for run data" : "no tool data recorded");
    return;
  }
  if (note) {
    text(note, report.all_tools ? "all tools selected" : "selected tools");
  }
  const assigned = Object.entries(report.assignments || {})
    .map(([role, tools]) => role + ": " + ((tools || []).join(", ") || "none"));
  const blockedEntries = Object.entries(report.blocked || {})
    .filter(([, tools]) => (tools || []).length)
    .map(([reason, tools]) => reason.replace(/_/g, " ") + ": " + tools.join(", "));
  const groups = [
    ["Enabled by user", report.enabled],
    ["Assigned workers", assigned],
    ["Planned as necessary", report.planned],
    ["Planned but disabled", report.planned_blocked],
    ["Actually attempted", report.invoked],
    ["Succeeded", report.succeeded],
    ["Returned usable evidence", report.usable_evidence],
    ["Failed", report.failed],
    ["Mixed success and failure", report.mixed],
    ["Blocked invocations", blockedEntries],
  ];
  groups.forEach(([label, values]) => {
    const row = document.createElement("div");
    row.className = "tool-report-row";
    const name = document.createElement("span");
    name.className = "sidebar-label";
    text(name, label);
    const value = document.createElement("span");
    text(value, (values || []).join(", ") || "none");
    row.appendChild(name);
    row.appendChild(value);
    host.appendChild(row);
  });
  if ((report.skipped || []).length) {
    const row = document.createElement("div");
    row.className = "tool-report-row is-warning";
    const name = document.createElement("span");
    name.className = "sidebar-label";
    text(name, "Skipped / blocked");
    const value = document.createElement("span");
    text(value, report.skipped.join("; "));
    row.appendChild(name);
    row.appendChild(value);
    host.appendChild(row);
  }
  const evidenceLimitations = (summary && summary.evidence_limitations) || [];
  if (evidenceLimitations.length) {
    const row = document.createElement("div");
    row.className = "tool-report-row";
    const name = document.createElement("span");
    name.className = "sidebar-label";
    text(name, "Evidence limitations");
    const value = document.createElement("span");
    text(value, evidenceLimitations.join("; "));
    row.appendChild(name);
    row.appendChild(value);
    host.appendChild(row);
  }
  const unmet = (summary && summary.unmet_requirements) || [];
  if (unmet.length) {
    const row = document.createElement("div");
    row.className = "tool-report-row is-warning";
    const name = document.createElement("span");
    name.className = "sidebar-label";
    text(name, "Not satisfied");
    const value = document.createElement("span");
    text(value, unmet.join("; "));
    row.appendChild(name);
    row.appendChild(value);
    host.appendChild(row);
  }
  if ((report.calls || []).length) {
    const details = document.createElement("details");
    details.className = "tool-report-calls";
    const summary = document.createElement("summary");
    text(summary, report.calls.length + " tool call" + (report.calls.length === 1 ? "" : "s"));
    details.appendChild(summary);
    report.calls.forEach((call) => {
      const row = document.createElement("div");
      const outcome = call.outcome || (call.ok ? "succeeded" : "failed");
      row.className = "tool-report-call "
        + (outcome.startsWith("succeeded") ? "is-ok" : "is-error");
      const head = document.createElement("strong");
      text(head, call.tool + " \u00b7 " + call.agent + " \u00b7 "
        + outcome.replace(/^blocked_/, "blocked by ").replace(/_/g, " "));
      const detail = document.createElement("span");
      text(detail, (call.summary || "")
        + (call.result_status ? " \u2014 status " + call.result_status : "")
        + (call.error ? " \u2014 " + call.error : ""));
      row.appendChild(head);
      row.appendChild(detail);
      details.appendChild(row);
    });
    host.appendChild(details);
  }
}

/* ------------------------------------------------------- response context */

/* Answer text where each `[n]` marker is a real button that opens the context
 * panel at that source. A marker without a matching source stays plain text
 * (`L.answerSegments` keeps it in the text run). Shared with Chat. */
function fillAnswer(node, body, maxIndex, onCitation) {
  if (!node) return;
  node.replaceChildren();
  L.answerSegments(body, maxIndex).forEach((segment) => {
    if (segment.type === "citation") {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "citation-marker";
      button.dataset.citation = String(segment.index);
      button.title = "Open source [" + segment.index + "] in the context panel";
      text(button, segment.text);
      button.addEventListener("click", () => onCitation(segment.index, button));
      node.appendChild(button);
      return;
    }
    const run = document.createElement("span");
    text(run, segment.text);
    node.appendChild(run);
  });
}

function renderResponseMeta(host, items, onAction) {
  if (!host) return;
  host.replaceChildren();
  (items || []).forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "response-meta-pill is-" + (item.tone || "muted");
    text(button, item.label);
    button.title = item.title || item.label;
    button.dataset.contextAction = item.action || "context";
    button.dataset.contextItem = item.id || "";
    button.addEventListener("click", () => onAction(item, button));
    host.appendChild(button);
  });
  host.classList.toggle("hidden", !(items || []).length);
}

function researchContextModel() {
  return L.researchContextModel({
    runId: state.runId,
    phase: state.phase,
    summary: state.summary,
    snapshot: state.snapshot,
    events: state.events,
    pattern: state.pattern,
    question: $("user-question") ? $("user-question").textContent : "",
  });
}

function openResearchContext(tab, trigger) {
  if (!state.runId) return;
  window.AthenaContext?.open(tab, "research", state.runId, trigger || null);
}

/* A clickable `[n]` marker in the answer: open the panel on that source. */
function openResearchSource(index, trigger) {
  if (!state.runId) return;
  window.AthenaContext?.openSource(String(index), "research", state.runId, trigger || null);
}

/* The explicit "Context" control in the result card head. */
function toggleResearchContext(trigger) {
  if (!state.runId) { toast("Open a research answer first."); return; }
  window.AthenaContext?.toggle("sources", "research", state.runId, trigger || null);
}

function renderResearchResponseMeta() {
  const host = $("research-response-meta");
  if (!host) return;
  if (!state.runId || state.loadingRunId) {
    host.replaceChildren();
    host.classList.add("hidden");
    return;
  }
  const model = researchContextModel();
  const items = L.responseMetaFromModel(model);
  renderResponseMeta(host, items, (item, button) => openResearchContext(item.action, button));
  host.classList.remove("hidden");
}

function researchContextProvider(runId) {
  if (!runId || runId !== state.runId || state.loadingRunId) return null;
  return researchContextModel();
}

window.AthenaContext?.register("research", researchContextProvider);

/* ---------------------------------------------------------------- snapshot */

async function loadSnapshot(runId) {
  const token = state.navToken;
  const snapshot = await api("/api/runs/" + runId);
  if (token !== state.navToken || state.runId !== runId) return;
  state.snapshot = snapshot;
  state.toolReport = snapshot.tool_report || null;
  renderBlackboard(snapshot);
  renderToolReport(snapshot.tool_report, state.summary);
  if (snapshot.final_report) renderReport(snapshot.final_report.value, snapshot.critique);
  renderResearchResponseMeta();
  window.AthenaContext?.refresh("research", runId);
  await loadMetrics(runId);
}

function renderEvidenceTable(box, evidence) {
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  table.className = "table";
  const head = document.createElement("tr");
  ["author", "source", "score", "preview"].forEach((label) => {
    const th = document.createElement("th");
    text(th, label);
    head.appendChild(th);
  });
  table.appendChild(head);
  (evidence || []).forEach((item) => {
    const tr = document.createElement("tr");
    [item.retrieved_by || "?", item.source_ref || "?", String(item.score ?? "")].forEach((value) => {
      const cell = document.createElement("td");
      text(cell, value);
      tr.appendChild(cell);
    });
    const cell = document.createElement("td");
    const full = String(item.text || "");
    if (!full) {
      text(cell, "(empty)");
    } else {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      text(summary, full.slice(0, 180) + (full.length > 180 ? "\u2026" : ""));
      const body = document.createElement("pre");
      body.className = "evidence-full";
      text(body, full);
      details.appendChild(summary);
      details.appendChild(body);
      cell.appendChild(details);
    }
    tr.appendChild(cell);
    table.appendChild(tr);
  });
  wrap.appendChild(table);
  box.appendChild(wrap);
}

function renderBlackboard(snapshot) {
  const host = $("blackboard-sections");
  host.replaceChildren();
  L.blackboardCards(snapshot).forEach((card) => {
    const box = document.createElement("div");
    box.className = "board-card";
    const title = document.createElement("h4");
    text(title, card.name + (card.version ? " \u00b7 v" + card.version : ""));
    box.appendChild(title);
    if (card.verdict) {
      const verdict = document.createElement("p");
      verdict.className = "verdict";
      text(verdict, card.verdict);
      box.appendChild(verdict);
    }
    if (card.name === "evidence" && snapshot.evidence) {
      renderEvidenceTable(box, snapshot.evidence);
    } else if (card.table) {
      const wrap = document.createElement("div");
      wrap.className = "table-wrap";
      const table = document.createElement("table");
      table.className = "table";
      const head = document.createElement("tr");
      card.columns.forEach((label) => {
        const th = document.createElement("th");
        text(th, label);
        head.appendChild(th);
      });
      table.appendChild(head);
      card.rows.forEach((row) => {
        const tr = document.createElement("tr");
        row.forEach((value) => {
          const cell = document.createElement("td");
          text(cell, value);
          tr.appendChild(cell);
        });
        table.appendChild(tr);
      });
      wrap.appendChild(table);
      box.appendChild(wrap);
    } else {
      const body = document.createElement("pre");
      text(body, (card.lines || []).join("\n"));
      box.appendChild(body);
    }
    host.appendChild(box);
  });
  const raw = document.createElement("pre");
  raw.className = "raw" + (state.rawJson ? "" : " hidden");
  text(raw, JSON.stringify(snapshot, null, 1));
  host.appendChild(raw);
}

function renderReport(report, critique) {
  state.hasReport = true;
  const citations = Array.isArray(report.citations) ? report.citations : [];
  fillAnswer($("report-answer"), report.answer || "(no answer)", citations.length,
    (index, button) => openResearchSource(index, button));

  const points = $("report-points");
  points.replaceChildren();
  (report.key_points || []).forEach((point) => {
    const item = document.createElement("li");
    text(item, point);
    points.appendChild(item);
  });

  const verdict = critique ? L.verdictFromCritique(critique) : { approved: null, severity: null };
  const diagnostic = L.critiqueDiagnostic(critique);
  const meta = $("report-meta");
  meta.replaceChildren();
  [["confidence " + report.confidence, ""],
   [L.verdictLabel(verdict.approved, verdict.severity), verdict.approved === false ? "bad" : "ok"],
   ["tools: " + ((report.tools_used || []).join(", ") || "none"), ""],
   ["sources " + ((report.citations || []).length), ""]].forEach((pair) => {
    const pill = document.createElement("span");
    pill.className = "pill " + pair[1];
    text(pill, pair[0]);
    meta.appendChild(pill);
  });

  const detail = $("critique-detail");
  detail.replaceChildren();
  const summary = state.summary || {};
  let diagnosticStatus = diagnostic.status;
  let diagnosticLabel = diagnostic.label;
  let diagnosticReason = diagnostic.reason;
  if (!critique && summary.critic_status === "failed") {
    diagnosticStatus = "failed";
    diagnosticLabel = "Critic failed";
    diagnosticReason = summary.critique_reason
      || "The critic failed before producing a critique; this is not an approval.";
  } else if (!critique && summary.critique_reason) {
    diagnosticReason = summary.critique_reason;
  }
  detail.className = "critique-detail "
    + (diagnosticStatus === "approved" ? "is-ok"
      : diagnosticStatus === "missing" || diagnosticStatus === "failed" ? "is-missing" : "");
  const heading = document.createElement("h3");
  text(heading, diagnosticLabel);
  detail.appendChild(heading);
  if (diagnostic.issues.length) {
    const list = document.createElement("ul");
    diagnostic.issues.forEach((issue) => {
      const item = document.createElement("li");
      text(item, issue);
      list.appendChild(item);
    });
    detail.appendChild(list);
  } else {
    const reason = document.createElement("p");
    text(reason, diagnosticReason);
    detail.appendChild(reason);
  }
  detail.classList.remove("hidden");

  const chips = $("report-citations");
  chips.replaceChildren();
  L.citationRows(report).forEach((row) => {
    const chip = document.createElement("a");
    chip.className = "citation-chip";
    chip.href = "#source-" + row.index;
    chip.title = "Trace citation [" + row.index + "] back to its tool call";
    chip.addEventListener("click", (event) => {
      if (!state.runId) return;
      event.preventDefault();
      loadProvenance(state.runId, row.index);
    });
    text(chip, "[" + row.index + "] " + row.source_ref);
    chips.appendChild(chip);
  });
  /* No second inline source list: the context panel holds the one
   * de-duplicated list, and the answer's [n] markers open it at a source. */
  renderResearchResponseMeta();
  window.AthenaContext?.refresh("research", state.runId);
}

/* ---------------------------------------------------------------- metrics */

function resetMetrics(message) {
  $("agent-metrics-body").replaceChildren();
  $("agent-metrics-table").classList.add("hidden");
  $("metrics-empty").classList.add("hidden");
  $("metrics-empty").textContent = "";
  $("heatmap").classList.add("hidden");
  $("heatmap").removeAttribute("src");
  $("heatmap-empty").classList.add("hidden");
  $("heatmap-empty").textContent = "";
  text($("metrics-note"), message || "");
}

function showMetricsEmpty(message) {
  $("agent-metrics-table").classList.add("hidden");
  $("heatmap").classList.add("hidden");
  $("heatmap").removeAttribute("src");
  $("heatmap-empty").classList.add("hidden");
  $("heatmap-empty").textContent = "";
  const empty = $("metrics-empty");
  empty.classList.remove("hidden");
  text(empty, message);
}

async function loadMetrics(runId) {
  if (!runId) {
    resetMetrics("");
    showMetricsEmpty("No run selected. Open a run to see recorded metrics.");
    resetTelemetry("");
    showTelemetryEmpty("No run selected. Open a run to see its cost, heat grid and waterfall.");
    return;
  }
  const token = state.navToken;
  resetMetrics("loading recorded metrics\u2026");
  loadTelemetry(runId);
  try {
    const data = await api("/api/runs/" + encodeURIComponent(runId) + "/metrics");
    if (token !== state.navToken) return;
    if (state.runId && state.runId !== runId) return;
    const rows = L.metricRows(data && data.rows);
    if (!rows.length) {
      text($("metrics-note"), "derived from the run event log");
      showMetricsEmpty(
        "No per-agent metrics were recorded for this run. Metrics are derived from event-log stage, model, tool, and A2A events; a run without those events has nothing to chart.",
      );
      return;
    }
    const body = $("agent-metrics-body");
    body.replaceChildren();
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      [row.agent, row.framework, row.stage, row.models, row.tokens_in, row.tokens_out,
       row.reasoning, row.tools, row.errors, row.a2a, row.evidence].forEach((value) => {
        const cell = document.createElement("td");
        text(cell, value);
        tr.appendChild(cell);
      });
      body.appendChild(tr);
    });
    $("agent-metrics-table").classList.remove("hidden");
    $("metrics-empty").classList.add("hidden");
    text($("metrics-note"), "recorded values, derived from event log");
    const heatmap = $("heatmap");
    heatmap.onerror = () => {
      heatmap.classList.add("hidden");
      const note = $("heatmap-empty");
      note.textContent = "Heatmap image unavailable; the per-agent table above still reflects recorded values.";
      note.classList.remove("hidden");
    };
    heatmap.src = "/api/runs/" + encodeURIComponent(runId) + "/heatmap.png?t=" + Date.now();
    heatmap.classList.remove("hidden");
    $("heatmap-empty").classList.add("hidden");
  } catch (error) {
    if (token !== state.navToken) return;
    text($("metrics-note"), "metrics unavailable");
    showMetricsEmpty("Could not load per-agent metrics: " + error.message);
  }
}

/* -------------------------------------------------------------- telemetry
 * Cost, heat grids, waterfall and provenance. The numbers are shaped by
 * static/telemetry.js (pure, node-tested); this section only builds elements
 * and fetches. Every value comes from the stored event log, and a missing
 * value is shown as a dash with the reason, never as a zero.
 */

const TELEMETRY_WATERFALL_WIDTH = 720;

function telemetryKit() {
  return window.AthenaTelemetry || null;
}

function hideTelemetryNote(id) {
  const node = $(id);
  if (!node) return;
  node.classList.add("hidden");
  text(node, "");
}

function resetTelemetry(message) {
  ["telemetry-metric-grid", "telemetry-bucket-grid", "telemetry-waterfall",
   "telemetry-cell-list"].forEach((id) => {
    const node = $(id);
    if (node) node.replaceChildren();
  });
  ["telemetry-metric-caption", "telemetry-bucket-caption", "telemetry-empty",
   "telemetry-waterfall-empty", "telemetry-cells-empty", "telemetry-cells"].forEach(hideTelemetryNote);
  const download = $("heatmap-download");
  if (download) {
    download.classList.add("hidden");
    download.removeAttribute("href");
  }
  const status = $("telemetry-status");
  if (status) text(status, message || "");
  return telemetryKit();
}

function showTelemetryEmpty(message) {
  const node = $("telemetry-empty");
  if (!node) return;
  text(node, message);
  node.classList.remove("hidden");
}

function renderHeatGrid(containerId, captionId, grid, kind, runId) {
  const T = telemetryKit();
  const host = $(containerId);
  if (!T || !host) return 0;
  host.replaceChildren();
  host.style.gridTemplateColumns = "";
  const caption = $(captionId);
  if (caption) {
    const basis = grid && grid.normalisation ? grid.normalisation : "";
    if (basis) {
      text(caption, kind + ": " + basis);
      caption.classList.remove("hidden");
    } else {
      hideTelemetryNote(captionId);
    }
  }
  if (!grid || grid.empty) return 0;
  host.style.gridTemplateColumns =
    "minmax(92px, max-content) repeat(" + grid.columns.length + ", minmax(50px, 1fr))";

  const corner = document.createElement("div");
  corner.className = "hg-corner";
  corner.setAttribute("role", "columnheader");
  text(corner, "agent");
  host.appendChild(corner);

  grid.columns.forEach((column) => {
    const head = document.createElement("div");
    head.className = "hg-col";
    head.setAttribute("role", "columnheader");
    head.title = column.title || column.label;
    text(head, column.label);
    host.appendChild(head);
  });

  grid.rows.forEach((row) => {
    const label = document.createElement("div");
    label.className = "hg-row-head";
    label.setAttribute("role", "rowheader");
    label.title = row.agent;
    text(label, row.agent);
    host.appendChild(label);
    grid.columns.forEach((column) => {
      const cell = row.cells[column.name];
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("role", "cell");
      button.className = "hg-cell" + (cell.hasValue ? "" : " is-empty")
        + (!cell.hasValue && cell.drillable ? " is-unpriced" : "");
      button.dataset.key = cell.key;
      button.style.background = cell.hasValue ? T.heatColour(cell.level) : "";
      const hint = cell.hasValue
        ? " \u2014 click for the events behind it"
        : (cell.drillable
          ? " \u2014 not priced from the stored events; click for the calls behind it"
          : " \u2014 not recorded");
      button.title = row.agent + " \u00b7 " + cell.label + ": " + cell.display + hint;
      text(button, cell.display);
      if (!cell.hasValue && !cell.drillable) {
        button.disabled = true;
      } else {
        button.addEventListener("click", () => {
          document.querySelectorAll(".hg-cell.is-selected")
            .forEach((node) => node.classList.remove("is-selected"));
          button.classList.add("is-selected");
          const target = kind === "stage"
            ? { agent: row.agent, bucket: Number(column.name) }
            : { agent: row.agent, metric: column.name };
          loadCellEvents(runId, target);
        });
      }
      host.appendChild(button);
    });
  });
  return grid.rows.length;
}

function renderWaterfall(spans, runId) {
  const T = telemetryKit();
  const host = $("telemetry-waterfall");
  if (!T || !host) return 0;
  host.replaceChildren();
  const geometry = T.waterfallGeometry(spans, TELEMETRY_WATERFALL_WIDTH);
  if (!geometry.bars.length) {
    const empty = $("telemetry-waterfall-empty");
    if (empty) {
      text(empty, "No span recorded both a start and an end, so there is nothing to lay out. "
        + "A span needs a stage_start/stage_end pair or a duration on its own event.");
      empty.classList.remove("hidden");
    }
    return 0;
  }
  hideTelemetryNote("telemetry-waterfall-empty");
  geometry.rows.forEach((row) => {
    const line = document.createElement("div");
    line.className = "wf-row";
    const label = document.createElement("span");
    label.className = "wf-agent";
    label.title = row.agent;
    text(label, row.agent);
    const track = document.createElement("div");
    track.className = "wf-track";
    geometry.bars.filter((bar) => bar.agent === row.agent).forEach((bar) => {
      const element = document.createElement("div");
      element.className = "wf-bar" + (bar.ok ? "" : " is-failed") + (bar.open ? " is-open" : "");
      element.style.left = ((bar.x / geometry.width) * 100).toFixed(3) + "%";
      element.style.width = Math.max(0.5, (bar.width / geometry.width) * 100).toFixed(3) + "%";
      element.title = bar.title + " \u00b7 starts at " + bar.offsetLabel + " \u00b7 " + bar.durationLabel;
      track.appendChild(element);
    });
    line.appendChild(label);
    line.appendChild(track);
    host.appendChild(line);
  });
  const axis = document.createElement("div");
  axis.className = "wf-axis";
  axis.appendChild(document.createElement("span"));
  const ticks = document.createElement("div");
  ticks.className = "wf-ticks";
  geometry.ticks.forEach((tick) => {
    const mark = document.createElement("span");
    mark.className = "wf-tick";
    mark.style.left = ((tick.x / geometry.width) * 100).toFixed(3) + "%";
    text(mark, tick.label);
    ticks.appendChild(mark);
  });
  axis.appendChild(ticks);
  host.appendChild(axis);
  return geometry.bars.length;
}

async function loadCellEvents(runId, target) {
  const T = telemetryKit();
  const box = $("telemetry-cells");
  const list = $("telemetry-cell-list");
  if (!T || !box || !list || !runId) return;
  const url = T.cellsUrl(runId, target);
  if (!url) return;
  box.classList.remove("hidden");
  list.replaceChildren();
  hideTelemetryNote("telemetry-cells-empty");
  const title = $("telemetry-cells-title");
  const heading = T.cellListTitle(target);
  if (title) text(title, heading + " \u00b7 loading\u2026");
  const showNote = (message) => {
    const note = $("telemetry-cells-empty");
    if (!note) return;
    text(note, message);
    note.classList.remove("hidden");
  };
  try {
    const data = await api(url);
    const rows = T.eventRows(data);
    if (title) text(title, heading + " \u00b7 " + rows.length + " event" + (rows.length === 1 ? "" : "s"));
    if (!rows.length) {
      showNote((data && data.reason) || "No stored event sits behind this cell.");
      return;
    }
    rows.forEach((row) => {
      const item = document.createElement("li");
      item.className = "cell-event" + (row.ok ? "" : " is-failed");
      const kind = document.createElement("span");
      kind.className = "cell-event-kind";
      text(kind, (row.ts ? row.ts + " \u00b7 " : "") + row.kind + (row.agent ? " \u00b7 " + row.agent : ""));
      const body = document.createElement("div");
      text(body, row.text + (row.detail ? " \u2014 " + row.detail : ""));
      item.appendChild(kind);
      item.appendChild(body);
      list.appendChild(item);
    });
  } catch (error) {
    if (title) text(title, heading);
    showNote("Could not load the events behind this cell: " + error.message);
  }
}

async function loadProvenance(runId, citation) {
  const T = telemetryKit();
  const panel = $("provenance-panel");
  const list = $("provenance-hops");
  if (!T || !panel || !list) return;
  const title = $("provenance-title");
  const note = $("provenance-empty");
  const heading = "Provenance of citation [" + citation + "]";
  panel.classList.remove("hidden");
  list.replaceChildren();
  if (note) {
    note.classList.add("hidden");
    text(note, "");
  }
  if (title) text(title, heading + " \u00b7 loading\u2026");
  const showNote = (message) => {
    if (!note) return;
    text(note, message);
    note.classList.remove("hidden");
  };
  try {
    const url = "/api/runs/" + encodeURIComponent(runId) + "/provenance?citation="
      + encodeURIComponent(String(citation));
    const data = await api(url);
    if (title) text(title, heading);
    const hops = T.provenanceHops(data);
    if (!hops.length) {
      showNote(T.provenanceUnavailable(data)
        || "This run stored no provenance chain for that citation.");
      return;
    }
    hops.forEach((hop) => {
      const item = document.createElement("li");
      const label = document.createElement("span");
      label.className = "prov-hop-label";
      text(label, hop.label);
      const detail = document.createElement("span");
      detail.className = "prov-hop-detail";
      text(detail, hop.detail);
      item.appendChild(label);
      item.appendChild(detail);
      list.appendChild(item);
    });
    const unavailable = T.provenanceUnavailable(data);
    if (unavailable) showNote(unavailable);
  } catch (error) {
    if (title) text(title, heading);
    showNote("Could not load provenance: " + error.message);
  }
}

async function loadTelemetry(runId) {
  const T = resetTelemetry("loading recorded telemetry\u2026");
  if (!T) {
    text($("telemetry-status"), "telemetry helpers did not load");
    showTelemetryEmpty("static/telemetry.js is missing, so the heat grid and waterfall cannot be drawn.");
    return;
  }
  if (!runId) {
    text($("telemetry-status"), "");
    showTelemetryEmpty("No run selected. Open a run to see its cost, heat grid and waterfall.");
    return;
  }
  const token = state.navToken;
  const base = "/api/runs/" + encodeURIComponent(runId);
  let failure = "";
  const [payload, heat] = await Promise.all([
    api(base + "/telemetry").catch((error) => { failure = failure || error.message; return null; }),
    api(base + "/heatmap.json").catch((error) => { failure = failure || error.message; return null; }),
  ]);
  if (token !== state.navToken) return;
  if (state.runId && state.runId !== runId) return;

  if (!payload && !heat) {
    text($("telemetry-status"), "telemetry unavailable");
    showTelemetryEmpty("Could not load telemetry for this run: " + failure);
    return;
  }

  const shape = T.shapeTelemetry(payload);
  const basis = shape.costBasis || {};
  const status = [T.telemetrySummary(shape)];
  if (basis.as_of) status.push("cost estimate at list prices as of " + basis.as_of);
  if (shape.missing.length) status.push("not recorded: " + shape.missing.join("; "));
  text($("telemetry-status"), status.filter(Boolean).join(" \u00b7 "));

  const metricGrid = T.metricGrid(heat);
  const bucketGrid = T.bucketGrid(heat);
  const metricRows = renderHeatGrid("telemetry-metric-grid", "telemetry-metric-caption",
    metricGrid, "agent \u00d7 metric", runId);
  const bucketRows = renderHeatGrid("telemetry-bucket-grid", "telemetry-bucket-caption",
    bucketGrid, "agent \u00d7 stage", runId);
  const bars = renderWaterfall(shape.spans, runId);

  if (!shape.agents.length && !metricRows && !bucketRows && !bars) {
    showTelemetryEmpty(shape.reason
      || (heat && heat.reason)
      || "This run stored no telemetry events, so there is nothing to chart.");
  } else if (!metricRows && !bucketRows && shape.agents.length) {
    showTelemetryEmpty("The heat map could not be loaded"
      + (failure ? " (" + failure + ")" : "")
      + "; the per-agent table above still shows the recorded values.");
  }

  const download = $("heatmap-download");
  const png = (heat && heat.png_fallback) || (base + "/heatmap.png");
  if (download && png) {
    download.href = png;
    download.classList.remove("hidden");
  }
}

function fillCompare(rows) {
  const body = $("compare-body");
  body.replaceChildren();
  rows.forEach((row) => {
    const summary = row.summary || {};
    const tr = document.createElement("tr");
    [row.name || summary.pattern, L.duration((summary.wall_s || 0) * 1000),
     summary.adk_tokens, (summary.a2a_bytes_out || 0) + "/" + (summary.a2a_bytes_in || 0),
     summary.evidence_count, summary.tool_calls, summary.confidence,
     L.verdictLabel(row.approved, summary.severity),
     (row.degraded || summary.degraded) ? "yes" : "no"].forEach((value) => {
      const cell = document.createElement("td");
      text(cell, value);
      tr.appendChild(cell);
    });
    body.appendChild(tr);
  });
}

async function compareAll() {
  const question = $("question").value.trim();
  if (!question) { toast("Type a question first."); return; }
  if (!hasResearchTools()) {
    toast("Select at least one research tool before comparing.");
    openResearchToolsMenu();
    return;
  }
  if (isLocked()) { toast(lockedReason()); return; }
  const ids = [];
  try {
    setBusy(true);
    for (const name of ["sequential", "parallel", "hierarchical", "p2p"]) {
      const data = await api("/api/runs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          pattern: name,
          tools: state.researchTools === null ? null : state.researchTools.slice(),
        }),
      });
      ids.push(data.run_id);
      await new Promise((resolve, reject) => {
        const source = new EventSource("/api/runs/" + data.run_id + "/stream");
        source.addEventListener("done", (message) => {
          source.close();
          try {
            const payload = JSON.parse(message.data);
            if (!payload || typeof payload.summary !== "object" || payload.summary === null) {
              throw new Error("malformed completion signal");
            }
            resolve();
          } catch (error) {
            reject(error);
          }
        });
        source.onerror = () => {
          source.close();
          reject(new Error("live updates stopped before a confirmed completion"));
        };
      });
    }
    const rows = await api("/api/compare?run=" + ids.join(","));
    fillCompare((rows.rows || []).map((row) => ({ ...row, name: row.pattern, summary: row })));
    showView("details");
    setTab("metrics");
  } catch (error) {
    const active = isActiveRunError(error);
    addLog("compare", "comparison failed: " + error.message, "error");
    markStartFailed();
    refreshHealth();
    toast(active ? ACTIVE_RUN_MESSAGE : "Compare failed: " + error.message);
  } finally {
    setBusy(false);
  }
}

/* ---------------------------------------------------------------- documents */

/* The preview drawer is read-only and reuses the context panel chrome. It
 * shows the indexed chunk text for one document; nothing is re-rendered. */
function closeDocPreview(trigger) {
  const shell = $("doc-preview");
  if (!shell) return;
  shell.classList.add("hidden");
  shell.setAttribute("aria-hidden", "true");
  state.docPreview = null;
  if (trigger && typeof trigger.focus === "function") {
    trigger.focus({ preventScroll: true });
  }
}

function renderDocPreview(data) {
  const body = $("doc-preview-body");
  if (!body) return;
  const chunks = Array.isArray(data.chunks) ? data.chunks : [];
  text($("doc-preview-title"), data.name || data.id || "Document");
  const pages = Array.isArray(data.pages) ? data.pages.filter((page) => page != null) : [];
  text($("doc-preview-subtitle"), [
    chunks.length + " of " + (data.chunk_count || chunks.length) + " chunk(s)",
    pages.length ? pages.length + " page(s)" : "",
    data.truncated ? "truncated" : "",
  ].filter(Boolean).join(" \u00b7 "));
  body.replaceChildren();
  if (!chunks.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    text(empty, "This document has no indexed chunk text to preview.");
    body.appendChild(empty);
    return;
  }
  chunks.forEach((chunk) => {
    const row = document.createElement("section");
    row.className = "doc-chunk";
    const head = document.createElement("div");
    head.className = "sidebar-label";
    text(head, "chunk " + (chunk.index == null ? "?" : chunk.index)
      + (chunk.page == null ? "" : " \u00b7 page " + chunk.page));
    const para = document.createElement("p");
    para.className = "doc-chunk-text";
    text(para, chunk.text || "");
    row.appendChild(head);
    row.appendChild(para);
    body.appendChild(row);
  });
  if (data.truncated) {
    const note = document.createElement("p");
    note.className = "hint";
    text(note, "The preview is size-capped; open the source file for the rest.");
    body.appendChild(note);
  }
}

async function openDocPreview(docId, trigger) {
  const shell = $("doc-preview");
  const body = $("doc-preview-body");
  if (!shell || !body) return;
  state.docPreview = { id: docId, trigger: trigger || null };
  shell.classList.remove("hidden");
  shell.setAttribute("aria-hidden", "false");
  text($("doc-preview-title"), docId);
  text($("doc-preview-subtitle"), "loading\u2026");
  body.replaceChildren();
  try {
    const data = await api("/api/library/documents/" + encodeURIComponent(docId));
    if (!state.docPreview || state.docPreview.id !== docId) return;
    renderDocPreview(data);
  } catch (error) {
    if (!state.docPreview || state.docPreview.id !== docId) return;
    text($("doc-preview-subtitle"), "");
    const note = document.createElement("p");
    note.className = "empty-state";
    text(note, "preview unavailable: " + error.message);
    body.replaceChildren(note);
  }
}

/* --------------------------------------------------------------- evaluation */

/* The Evaluation nav item only exists when there is real harness output. */
async function refreshEvalNav() {
  const nav = $("nav-eval");
  if (!nav) return false;
  try {
    const data = await api("/api/eval/runs");
    const has = Boolean(((data && data.runs) || []).length);
    nav.classList.toggle("hidden", !has);
    return has;
  } catch (_) {
    nav.classList.add("hidden");
    return false;
  }
}

function evalCell(value, formatter) {
  const cell = document.createElement("td");
  const text_ = value == null || value === "" ? "\u2014" : formatter(value);
  text(cell, text_);
  return cell;
}

function renderEvaluation(summary) {
  const rows = (summary && summary.patterns) || [];
  const body = $("eval-body");
  const table = $("eval-table");
  const empty = $("eval-empty");
  const charts = $("eval-charts");
  if (!body || !table) return;
  body.replaceChildren();
  if (charts) charts.replaceChildren();
  if (!rows.length) {
    table.classList.add("hidden");
    if (empty) {
      empty.classList.remove("hidden");
      text(empty, "No scored runs in this batch. "
        + ((summary && summary.problems) || []).join("; "));
    }
    return;
  }
  if (empty) empty.classList.add("hidden");
  table.classList.remove("hidden");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    text(name, row.pattern);
    tr.appendChild(name);
    tr.appendChild(evalCell(row.runs, (value) => String(value)));
    tr.appendChild(evalCell(row.success_rate == null ? null : row.success_rate,
      (value) => (row.successes || 0) + "/" + (row.scored || 0)
        + " (" + Math.round(Number(value) * 100) + "%)"));
    tr.appendChild(evalCell(row.latency_median_s,
      (value) => Number(value).toFixed(1) + " s"));
    tr.appendChild(evalCell(row.tokens_median,
      (value) => Math.round(Number(value)).toLocaleString()));
    tr.appendChild(evalCell(row.cost_median_usd,
      (value) => "$" + Number(value).toFixed(4)));
    tr.appendChild(evalCell(row.citation_precision_median,
      (value) => Number(value).toFixed(2)));
    body.appendChild(tr);
  });
  if (charts && (summary.charts || []).length) {
    summary.charts.forEach((chart) => {
      const link = document.createElement("a");
      link.className = "eval-chart";
      link.href = chart.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      const image = document.createElement("img");
      image.src = chart.href;
      image.alt = chart.name;
      image.loading = "lazy";
      link.appendChild(image);
      charts.appendChild(link);
    });
  }
}

async function loadEvaluation(runId) {
  const picker = $("eval-run-picker");
  const status = $("eval-status");
  try {
    const data = await api("/api/eval/runs");
    const runs = (data && data.runs) || [];
    const nav = $("nav-eval");
    if (nav) nav.classList.toggle("hidden", !runs.length);
    if (!runs.length) {
      if (status) text(status, "No evaluation results are present yet.");
      renderEvaluation({ patterns: [] });
      if (picker) picker.replaceChildren();
      return;
    }
    const wanted = runId || (picker && picker.value) || runs[0].id;
    if (picker) {
      picker.replaceChildren();
      runs.forEach((run) => {
        const option = document.createElement("option");
        option.value = run.id;
        text(option, run.id + " \u00b7 " + run.runs + " run(s)");
        option.selected = run.id === wanted;
        picker.appendChild(option);
      });
    }
    const summary = await api("/api/eval/summary?run=" + encodeURIComponent(wanted));
    if (status) {
      text(status, [
        wanted,
        (summary.records || 0) + " recorded",
        (summary.scored || 0) + " scored",
        ...((summary.problems || []).length ? [(summary.problems || []).length + " problem(s)"] : []),
      ].join(" \u00b7 "));
    }
    renderEvaluation(summary);
  } catch (error) {
    if (status) text(status, "evaluation unavailable: " + error.message);
    renderEvaluation({ patterns: [] });
  }
}

/* ---------------------------------------------------------------- tools */

async function loadDocuments() {
  const host = $("documents-content");
  const status = $("documents-status");
  if (!host) return;
  host.replaceChildren();
  if (status) text(status, "loading indexed documents\u2026");
  try {
    const data = await api("/api/settings/library");
    const rag = (data && data.rag) || {};
    const documents = Array.isArray(rag.documents) ? rag.documents : [];
    if (!documents.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      text(empty, rag.chunks
        ? "The index has chunks but no readable document metadata."
        : "No documents are indexed yet. Add files to data/corpus and rebuild in Settings \u2192 Library.");
      host.appendChild(empty);
    } else {
      const list = document.createElement("ul");
      list.className = "documents-list";
      documents.forEach((entry) => {
        const item = document.createElement("li");
        item.className = "document-row";
        const name = document.createElement("strong");
        text(name, entry.name || entry.id || "document");
        const meta = document.createElement("span");
        text(meta, (entry.chunks || 0) + " chunks"
          + (entry.page_count ? " \u00b7 " + entry.page_count + " pages" : ""));
        const open = document.createElement("button");
        open.type = "button";
        open.className = "link-button document-open";
        text(open, "preview");
        open.title = "Read the indexed text of this document";
        open.addEventListener("click", () => {
          openDocPreview(entry.id || entry.name || "", open);
        });
        item.appendChild(name);
        item.appendChild(meta);
        item.appendChild(open);
        list.appendChild(item);
      });
      host.appendChild(list);
    }
    if (status) {
      text(status, (rag.chunks || 0) + " indexed chunk"
        + (rag.chunks === 1 ? "" : "s") + " \u00b7 "
        + documents.length + " document" + (documents.length === 1 ? "" : "s")
        + (rag.corpus_dir ? " \u00b7 " + rag.corpus_dir : ""));
    }
  } catch (error) {
    if (status) text(status, "documents unavailable: " + error.message);
  }
}

async function loadTools() {
  const host = $("tools-list");
  try {
    const data = await api("/api/tools");
    host.replaceChildren();
    (data.servers || []).forEach((server) => {
      const box = document.createElement("div");
      box.className = "board-card";
      const title = document.createElement("h4");
      text(title, server.name);
      box.appendChild(title);
      const list = document.createElement("ul");
      (server.tools || []).forEach((tool) => {
        const item = document.createElement("li");
        text(item, tool.name + " - " + tool.description);
        list.appendChild(item);
      });
      box.appendChild(list);
      host.appendChild(box);
    });
    if (!(data.servers || []).length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      text(empty, "tools unavailable (the MCP servers need a permissive shell)");
      host.appendChild(empty);
    }
  } catch (error) {
    text(host, "tools unavailable: " + error.message);
  }
}

function renderHowDiagram() {
  const spec = L.diagramFor(state.pattern);
  D.render($("how-diagram"), spec, { nodes: {}, edges: {} }, {});
  updateDiagramScrollHints();
  const legend = $("diagram-legend");
  legend.replaceChildren();
  ["ADK agents plan and gather evidence",
   "CrewAI roles answer over A2A on :9001",
   "MCP tools: RAG, web search, calculator",
   "Blackboard: shared state, read and written by every stage",
   "Pattern: " + spec.pattern + (spec.blackboardAuditOnly ? " (blackboard is audit-only)" : "")].forEach((label) => {
    const item = document.createElement("li");
    text(item, label);
    legend.appendChild(item);
  });
}

/* ---------------------------------------------------------------- wiring */

$("composer-pattern").addEventListener("change", (event) => {
  setPattern(event.target.value);
});
const patternTrigger = $("pattern-trigger");
const patternMenu = $("pattern-menu");
if (patternTrigger) {
  patternTrigger.addEventListener("click", (event) => {
    event.stopPropagation();
    if (patternMenu && patternMenu.classList.contains("hidden")) openPatternMenu();
    else closePatternMenu(false);
  });
  patternTrigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      openPatternMenu();
    }
    if (event.key === "Escape") closePatternMenu(false);
  });
}
if (patternMenu) {
  patternMenu.addEventListener("keydown", (event) => {
    const options = patternMenuOptions();
    const index = options.indexOf(document.activeElement);
    if (event.key === "Escape") {
      event.preventDefault();
      closePatternMenu(true);
      return;
    }
    if (event.key === "Tab") {
      closePatternMenu(true);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!options.length) return;
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      const next = index < 0
        ? (step > 0 ? 0 : options.length - 1)
        : (index + step + options.length) % options.length;
      options[next].focus();
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      if (!options.length) return;
      event.preventDefault();
      options[event.key === "Home" ? 0 : options.length - 1].focus();
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      if (index < 0) return;
      event.preventDefault();
      choosePatternOption(options[index]);
    }
  });
  patternMenu.querySelectorAll(".pattern-option").forEach((option) => {
    option.addEventListener("click", () => choosePatternOption(option));
  });
}
document.querySelectorAll("#pattern-chips .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    setPattern(chip.dataset.pattern);
  });
});
setPattern(state.pattern, false);

const researchToolsTrigger = $("research-tools-trigger");
const researchToolsMenu = $("research-tools-menu");
if (researchToolsTrigger) {
  researchToolsTrigger.addEventListener("click", (event) => {
    event.stopPropagation();
    if (researchToolsMenu && researchToolsMenu.classList.contains("hidden")) {
      openResearchToolsMenu();
    } else {
      closeResearchToolsMenu(false);
    }
  });
  researchToolsTrigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      openResearchToolsMenu();
    }
    if (event.key === "Escape") closeResearchToolsMenu(false);
  });
}
if (researchToolsMenu) {
  researchToolsMenu.addEventListener("keydown", handleResearchToolsKey);
  researchToolsMenu.addEventListener("click", (event) => event.stopPropagation());
}
renderResearchToolMenu();
syncResearchTools();

document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => {
    if (window.innerWidth <= 680) setSidebar(true, false);
    if (item.dataset.view === "settings") {
      window.AthenaSettings?.open();
      return;
    }
    showView(item.dataset.view);
  });
});

document.querySelectorAll("#run-tabs .tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    if (tab.dataset.tab === "answer") { showAnswerTab(); return; }
    setTab(tab.dataset.tab);
  });
});

$("new-chat").addEventListener("click", newChat);
$("reconnect-run").addEventListener("click", reconnectRun);
$("report-details").addEventListener("click", () => showView("details"));
$("details-back").addEventListener("click", () => showView("run"));
$("activity-toggle").addEventListener("click", toggleActivityTrace);
$("documents-refresh").addEventListener("click", loadDocuments);
$("previous-run-dismiss").addEventListener("click", () => {
  state.previousRun = null;
  renderStatus();
});
$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  startRun();
});
$("question").addEventListener("input", () => {
  autoGrow();
  if (!$("examples-popover").classList.contains("hidden")) updateExampleSelection();
});
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    startRun();
  }
});

$("compare-all").addEventListener("click", compareAll);
$("examples-toggle").addEventListener("click", (event) => {
  event.stopPropagation();
  const panel = $("examples-popover");
  if (panel.classList.contains("hidden")) openExamples();
  else closeExamples(false);
});
$("examples-toggle").addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    openExamples();
  }
  if (event.key === "Escape") closeExamples(false);
});
$("examples-list").addEventListener("keydown", handleExamplesKey);
document.addEventListener("pointerdown", (event) => {
  const panel = $("examples-popover");
  const toggle = $("examples-toggle");
  if (panel && !panel.classList.contains("hidden")
      && !panel.contains(event.target) && !toggle.contains(event.target)) {
    closeExamples(false);
  }
  const picker = $("pattern-picker");
  const menu = $("pattern-menu");
  if (picker && menu && !menu.classList.contains("hidden") && !picker.contains(event.target)) {
    closePatternMenu(false);
  }
  const toolsPicker = $("research-tools-picker");
  const toolsMenu = $("research-tools-menu");
  if (toolsPicker && toolsMenu && !toolsMenu.classList.contains("hidden")
      && !toolsPicker.contains(event.target)) {
    closeResearchToolsMenu(false);
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const preview = $("doc-preview");
  if (preview && !preview.classList.contains("hidden")) {
    event.preventDefault();
    closeDocPreview();
    return;
  }
  const panel = $("examples-popover");
  if (panel && !panel.classList.contains("hidden")) {
    event.preventDefault();
    closeExamples(true);
    return;
  }
  const toolsMenu = $("research-tools-menu");
  if (toolsMenu && !toolsMenu.classList.contains("hidden")) {
    event.preventDefault();
    closeResearchToolsMenu(true);
    return;
  }
  const menu = $("pattern-menu");
  if (menu && !menu.classList.contains("hidden")) {
    event.preventDefault();
    closePatternMenu(true);
  }
});
document.addEventListener("athena:view", () => {
  closeExamples(false);
  closePatternMenu(false);
  closeResearchToolsMenu(false);
});
window.addEventListener("resize", scheduleExamplePosition);
document.querySelector(".main")?.addEventListener("scroll", scheduleExamplePosition, { passive: true });
document.querySelector(".main")?.addEventListener("scroll", () => {
  if (state.scrollTimer) clearTimeout(state.scrollTimer);
  state.scrollTimer = setTimeout(() => {
    state.scrollTimer = null;
    saveResearchScroll();
  }, 150);
}, { passive: true });
window.addEventListener("beforeunload", saveResearchScroll);
$("replay-toggle").addEventListener("click", () => {
  const panel = $("replay-panel");
  const hidden = panel.classList.toggle("hidden");
  $("replay-toggle").setAttribute("aria-expanded", hidden ? "false" : "true");
});
document.querySelectorAll("#replay-speed-row .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll("#replay-speed-row .chip").forEach((other) => other.classList.remove("selected"));
    chip.classList.add("selected");
    state.speed = Number(chip.dataset.speed);
  });
});
$("replay-speed-row").querySelector('[data-speed="1"]').classList.add("selected");

$("copy-markdown").addEventListener("click", () => {
  const body = $("report-answer").textContent + "\n\n"
    + Array.from($("report-points").children).map((item) => "- " + item.textContent).join("\n");
  navigator.clipboard.writeText(body)
    .then(() => toast("Markdown copied."), () => toast("Copy failed."));
});
$("toggle-raw").addEventListener("click", () => {
  state.rawJson = !state.rawJson;
  document.querySelectorAll(".raw").forEach((node) => node.classList.toggle("hidden", !state.rawJson));
});
$("telemetry-cells-close").addEventListener("click", () => {
  const box = $("telemetry-cells");
  if (box) box.classList.add("hidden");
});
$("provenance-close").addEventListener("click", () => {
  const panel = $("provenance-panel");
  if (panel) panel.classList.add("hidden");
});
$("research-context-toggle").addEventListener("click", (event) => {
  toggleResearchContext(event.currentTarget);
});
$("doc-preview-close").addEventListener("click", () => closeDocPreview());
$("doc-preview-backdrop").addEventListener("click", () => closeDocPreview());
$("eval-run-picker").addEventListener("change", (event) => {
  loadEvaluation(event.currentTarget.value);
});
$("sidebar-toggle").addEventListener("click", () => {
  setSidebar(!document.body.classList.contains("sidebar-collapsed"));
});
$("sidebar-backdrop").addEventListener("click", () => setSidebar(true));
$("history-refresh").addEventListener("click", loadHistory);
$("history-more").addEventListener("click", () => {
  state.historyLimit += HISTORY_LIMIT;
  renderHistory();
});
$("tools-refresh").addEventListener("click", loadTools);
$("theme-toggle").addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
});
$("banner").addEventListener("click", () => {
  state.noticeDismissed = true;
  banner("", false);
});

/* Shared surface for the chat, settings and tutorial modules. */
window.AthenaUi = {
  api,
  toast,
  text,
  $,
  showView,
  applyTheme,
  applySize,
  openRun,
  activityRow,
  refreshHealth,
  isBusy: () => isLocked() || state.busy || Boolean(state.activeRun),
  currentView: () => state.view,
};

/* boot: theme, size, deep links */
try {
  applyTheme(localStorage.getItem("athena.theme") || "dark");
  applySize(localStorage.getItem("athena.presentation") === "1");
} catch (_) {
  applyTheme("dark");
  applySize(false);
}
try {
  let storedSidebar = localStorage.getItem("athena.sidebar");
  const startCollapsed = window.innerWidth <= 680
    ? true
    : storedSidebar === "collapsed";
  setSidebar(startCollapsed, false);
} catch (_) {
  setSidebar(window.innerWidth <= 680, false);
}
let narrowViewport = window.innerWidth <= 680;
window.addEventListener("resize", () => {
  const narrow = window.innerWidth <= 680;
  if (narrow && !narrowViewport) setSidebar(true, false);
  narrowViewport = narrow;
  updateSidebarBackdrop();
  updateDiagramScrollHints();
});

function parseHash() {
  const raw = (window.location.hash || "").replace(/^#/, "");
  const parts = raw.split("&");
  const view = parts[0] || "run";
  const params = {};
  parts.slice(1).forEach((pair) => {
    const [key, value] = pair.split("=");
    if (key) params[key] = decodeURIComponent(value || "");
  });
  return { view, params };
}

const boot = parseHash();
const bootTab = ["answer", "activity", "blackboard", "metrics"].includes(boot.params.tab)
  ? boot.params.tab : "answer";
setTab(bootTab);
renderTabs();
renderLiveMap();
autoGrow();
refreshHealth();
loadQuestions().catch(() => {});
loadHistory();
loadReplays().catch(() => {});
loadResearchTools().catch(() => {});
/* The Evaluation nav item only exists once the harness has written results. */
refreshEvalNav().catch(() => {});

if (boot.view.startsWith("replay=")) {
  const name = boot.view.slice(7);
  const at = boot.params.at === undefined ? 1 : Number(boot.params.at);
  openReplay(name, { at: Number.isFinite(at) ? at : 1, tab: bootTab });
} else if (boot.view === "chat") {
  showView("chat");
} else if (["settings", "how", "documents"].includes(boot.view)) {
  if (boot.view === "settings") {
    showView("run");
    setTimeout(() => window.AthenaSettings?.open(), 0);
  } else {
    showView(boot.view);
  }
} else if (bootTab !== "answer") {
  showView("details");
  setTab(bootTab);
} else {
  showView("run");
}
setInterval(refreshHealth, 20000);
