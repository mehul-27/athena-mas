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

const state = {
  view: "run", pattern: L.DEFAULT_PATTERN, runId: null, events: [],
  source: null, retries: 0, replaying: false, playback: false, bundles: [], presets: [],
  profiles: [], active: "", editing: "", rawJson: false, busy: false,
  runs: [], historyLimit: HISTORY_LIMIT, speed: 1,
  tab: L.tabReducer(undefined, { type: "select", tab: "answer" }),
  request: { status: "idle", message: "", rows: [] },
  health: null, healthError: false, noticeKey: "", noticeDismissed: false,
  hasReport: false, mode: null, startFailure: false,
  phase: "idle", connection: "idle", activeRun: null, summary: null,
  seenEvents: new Set(), lastSeq: 0, lastEventAt: 0, stalled: false,
  watchdog: null, endTimer: null, logChecking: false, logError: false,
  streamToken: 0, previousRun: null, phaseDetail: "",
};

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
}

function applySize(presentation) {
  document.documentElement.dataset.size = presentation ? "presentation" : "normal";
  $("presentation-toggle").classList.toggle("on", presentation);
  try { localStorage.setItem("athena.presentation", presentation ? "1" : "0"); } catch (_) { /* ignore */ }
}

function showView(name) {
  state.view = name;
  ["run", "details", "how", "documents", "settings"].forEach((view) => {
    const node = $("view-" + view);
    if (node) node.classList.toggle("hidden", view !== name);
  });
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.view === name);
  });
  if (name === "run") { setTab("answer"); renderLiveMap(); }
  if (name === "details") {
    if (state.tab.tab === "answer") setTab("activity");
    renderLiveMap();
  }
  if (name === "how") { renderHowDiagram(); loadTools(); }
  if (name === "settings") loadSettings();
  try { history.replaceState(null, "", "#" + name + (state.replaying ? "&replay=" + state.runId : "")); } catch (_) { /* ignore */ }
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
  const details = $("exchange-details");
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
      message = "Researching. Live agent events are shown in Run details.";
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
  details.classList.toggle("hidden", !state.runId);
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

function chooseQuestion(value) {
  $("question").value = String(value || "");
  autoGrow();
  $("examples-popover").classList.add("hidden");
  $("examples-toggle").setAttribute("aria-expanded", "false");
  $("question").focus();
}

async function loadQuestions() {
  const data = await api("/api/questions");
  const popover = $("examples-popover");
  popover.replaceChildren();
  (data.demo || []).concat((data.eval || []).slice(0, 12)).forEach((item) => {
    const row = document.createElement("li");
    row.setAttribute("role", "option");
    text(row, (item.id ? item.id + " - " : "") + String(item.text));
    row.addEventListener("click", () => chooseQuestion(item.text));
    popover.appendChild(row);
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
  document.querySelectorAll("#pattern-chips .chip").forEach((chip) => {
    chip.classList.toggle("selected", chip.dataset.pattern === next);
  });
  if (render) renderLiveMap();
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
  const button = $("run-button");
  button.disabled = busy;
  text(button, busy ? "running..." : "Run");
  updateLockedControls();
  renderStatus();
}

function resetReport(message) {
  state.hasReport = false;
  text($("report-answer"), message || "");
  $("report-points").replaceChildren();
  $("report-meta").replaceChildren();
  $("critique-detail").replaceChildren();
  $("critique-detail").className = "critique-detail hidden";
  $("report-citations").replaceChildren();
  $("sources-list").replaceChildren();
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
  if (isLocked()) { toast(lockedReason()); return; }
  closeStream();
  stopWatchdog();
  clearEndTimer();
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
  state.seenEvents.clear();
  state.lastSeq = 0;
  state.lastEventAt = 0;
  state.stalled = false;
  state.logChecking = false;
  state.logError = false;
  state.previousRun = null;
  clearStartFailure();
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
  showView("run");
  showEmpty();
  renderStatus();
  $("question").focus();
}

async function startRun() {
  const submittedQuestion = $("question").value;
  if (!submittedQuestion.trim()) { toast("Type a question first."); return; }
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
      body: JSON.stringify({ question: submittedQuestion, pattern: state.pattern }),
    });
    if (!L.validRunId(data && data.run_id)) {
      throw new Error("server did not return a valid run id");
    }
    $("question").value = "";
    autoGrow();
    showView("run");
    showExchange(submittedQuestion);
    beginRun(data.run_id, false, "live");
    resetReport("Athena is researching this question\u2026");
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
  state.replaying = replaying;
  state.mode = mode || (replaying ? "replay" : "live");
  state.events = [];
  state.seenEvents = new Set();
  state.lastSeq = 0;
  state.lastEventAt = Date.now();
  state.stalled = false;
  state.summary = null;
  state.retries = 0;
  state.logChecking = false;
  state.logError = false;
  state.activeRun = state.mode === "live" ? { id: runId } : null;
  state.connection = state.mode === "live" ? "connecting" : "idle";
  state.phase = state.mode === "live" ? "starting" : state.mode;
  state.phaseDetail = "";
  resetReport("");
  $("replay-badge").classList.toggle("hidden", !replaying);
  $("replay-speed-row").classList.toggle("hidden", !replaying);
  text($("footer-run"), "run: " + runId);
  $("link-json").href = "/api/runs/" + runId;
  $("link-log").href = "/api/runs/" + runId + "/log";
  $("event-log").replaceChildren();
  $("timeline-body").replaceChildren();
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

function logEvent(event) {
  if (event.kind === "a2a_call") {
    const extra = event.extra || {};
    addLog("A2A", event.agent + " " + L.bytes(extra.request_bytes) + " out / "
      + L.bytes(extra.response_bytes) + " in", "a2a");
  } else if (event.kind === "tool_call") {
    addLog("tool", event.summary || event.agent, "tool");
  } else if (event.kind === "blackboard_write") {
    addLog("board", event.summary || event.agent);
  } else if (event.kind === "error") {
    addLog("error", (event.error_type || "error") + ": " + (event.error_message || ""), "error");
  } else if (event.kind === "stage_end") {
    addLog("stage", event.agent + " " + L.duration(event.duration_ms) + " \u00b7 "
      + L.tokens(event.tokens_in, event.tokens_out));
  }
}

function renderTimeline() {
  const body = $("timeline-body");
  body.replaceChildren();
  L.reduceTimeline(state.events).forEach((row) => {
    const tr = document.createElement("tr");
    tr.className = row.status;
    [row.agent, row.framework.toUpperCase(), row.status, L.duration(row.ms),
     L.tokens(row.tokens_in, row.tokens_out), row.stop_reason || ""].forEach((value) => {
      const cell = document.createElement("td");
      text(cell, value);
      tr.appendChild(cell);
    });
    body.appendChild(tr);
  });
}

function renderLiveMap() {
  const spec = L.diagramFor(state.pattern);
  const view = L.diagramState(state.events, state.pattern);
  D.render($("live-map"), spec, view, { compact: true });
}

/* ---------------------------------------------------------------- replay */

async function openReplay(name, options) {
  const opts = options || {};
  if (isLocked()) { toast(lockedReason()); return; }
  state.playback = true;
  try {
    const bundle = await api("/api/replay/" + name);
    const at = typeof opts.at === "number" ? Math.max(0, Math.min(1, opts.at)) : 1;
    showView("run");
    setPattern((bundle.summary && bundle.summary.pattern) || state.pattern, false);
    beginRun(bundle.run_id, true);
    state.summary = bundle.summary || null;
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
  showView("run");
  try {
    const snapshot = await api("/api/runs/" + runId);
    setPattern(
      (snapshot.task && snapshot.task.value.pattern) || state.pattern,
      false,
    );
    beginRun(runId, false, "history");
    state.summary = snapshot.summary || null;
    try { history.replaceState(null, "", "#run"); } catch (_) { /* ignore */ }
    showExchange(snapshot.task && snapshot.task.value ? snapshot.task.value.question : "");
    resetReport("Loading run\u2026");
    renderBlackboard(snapshot);
    if (snapshot.final_report) renderReport(snapshot.final_report.value, snapshot.critique);
    if (!state.hasReport) resetReport("No report is available for this run.");
    await loadMetrics(runId);
    renderLiveMap();
    setRunning(false);
  } catch (error) {
    toast("Could not open run: " + error.message);
  }
}

/* ---------------------------------------------------------------- snapshot */

async function loadSnapshot(runId) {
  const snapshot = await api("/api/runs/" + runId);
  renderBlackboard(snapshot);
  if (snapshot.final_report) renderReport(snapshot.final_report.value, snapshot.critique);
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
  text($("report-answer"), report.answer || "(no answer)");

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
    text(chip, "[" + row.index + "] " + row.source_ref);
    chips.appendChild(chip);
  });

  const list = $("sources-list");
  list.replaceChildren();
  L.citationRows(report).forEach((row) => {
    const item = document.createElement("li");
    item.id = "source-" + row.index;
    text(item, row.source_ref + " \u2014 " + row.quote);
    list.appendChild(item);
  });
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
  if (!runId) { resetMetrics(""); showMetricsEmpty("No run selected. Open a run to see recorded metrics."); return; }
  resetMetrics("loading recorded metrics\u2026");
  try {
    const data = await api("/api/runs/" + encodeURIComponent(runId) + "/metrics");
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
    text($("metrics-note"), "metrics unavailable");
    showMetricsEmpty("Could not load per-agent metrics: " + error.message);
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
  if (isLocked()) { toast(lockedReason()); return; }
  const ids = [];
  try {
    setBusy(true);
    for (const name of ["sequential", "parallel", "hierarchical", "p2p"]) {
      const data = await api("/api/runs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, pattern: name }),
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

/* ---------------------------------------------------------------- tools */

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

/* ---------------------------------------------------------------- settings */

function setRequest(action) {
  state.request = L.requestState(state.request, action);
  const busy = L.isBusy(state.request);
  ["profile-save", "profile-test", "profile-activate", "profile-delete"].forEach((id) => {
    const button = $(id);
    if (button) button.disabled = busy;
  });
  text($("profile-status"), state.request.message);
  renderTestResults();
}

function renderTestResults() {
  const host = $("test-result");
  host.replaceChildren();
  const rows = state.request.rows || [];
  if (!rows.length) {
    if (state.request.status === "error" || state.request.status === "timeout") {
      const message = document.createElement("p");
      message.className = "error-text";
      text(message, state.request.message);
      host.appendChild(message);
    }
    return;
  }
  const table = document.createElement("table");
  table.className = "table";
  const head = document.createElement("tr");
  ["path", "ok", "host", "tokens", "error"].forEach((label) => {
    const th = document.createElement("th");
    text(th, label);
    head.appendChild(th);
  });
  table.appendChild(head);
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    [row.path, row.ok ? "ok" : "failed", row.host,
     (row.tokens_in || 0) + "/" + (row.tokens_out || 0),
     row.error_type ? row.error_type + ": " + (row.error || "") : ""].forEach((value) => {
      const cell = document.createElement("td");
      text(cell, value);
      tr.appendChild(cell);
    });
    table.appendChild(tr);
  });
  host.appendChild(table);
}

async function loadSettings() {
  try {
    const [profiles, presets, about] = await Promise.all([
      api("/api/settings/profiles"), api("/api/settings/presets"), api("/api/settings/about-me"),
    ]);
    state.profiles = profiles.profiles || [];
    state.active = profiles.active || "";
    state.presets = presets.presets || [];
    $("about-me").value = about.about_me || "";
    const select = $("profile-preset");
    if (!select.options.length) {
      select.appendChild(new Option("(choose a preset)", ""));
      state.presets.forEach((preset) => select.appendChild(new Option(preset.name, preset.id)));
    }
    renderProfiles();
    text($("settings-state"), state.active ? "" : "no active provider");
    if (!state.active && state.profiles.length === 0) {
      text($("settings-state"), "No provider configured. Add one to run live.");
    }
  } catch (error) {
    text($("settings-state"), "cannot load settings: " + error.message);
  }
}

function renderProfiles() {
  const body = $("profiles-body");
  body.replaceChildren();
  state.profiles.forEach((profile) => {
    const tr = document.createElement("tr");
    const host = String(profile.api_base || "").replace(/^https?:\/\//, "").split("/")[0];
    [profile.name, profile.model, host,
     profile.has_llm_key ? "stored" : "missing",
     profile.id === state.active ? "active" : ""].forEach((value) => {
      const cell = document.createElement("td");
      text(cell, value);
      tr.appendChild(cell);
    });
    const actions = document.createElement("td");
    const activate = document.createElement("button");
    activate.type = "button";
    activate.className = "link-button";
    text(activate, "activate");
    activate.title = "Activate this saved profile";
    activate.addEventListener("click", () => activateProfile(profile.id));
    actions.appendChild(activate);
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "link-button";
    text(edit, "edit");
    edit.addEventListener("click", () => editProfile(profile));
    actions.appendChild(edit);
    const replace = document.createElement("button");
    replace.type = "button";
    replace.className = "link-button";
    text(replace, "replace key");
    replace.addEventListener("click", () => {
      $("profile-key").value = "";
      $("profile-key").focus();
    });
    actions.appendChild(replace);
    tr.appendChild(actions);
    body.appendChild(tr);
  });
}

function editProfile(profile) {
  state.editing = profile.id;
  text($("profile-form-title"), "Edit " + profile.name);
  $("profile-name").value = profile.name || "";
  $("profile-api-base").value = profile.api_base || "";
  $("profile-model").value = profile.model || "";
  $("profile-reasoning").value = profile.reasoning_effort || "";
  $("profile-search").value = profile.search_provider || "duckduckgo";
  $("profile-search-endpoint").value = profile.search_endpoint || "";
  $("profile-key").value = "";  // never repopulate a stored key
}

function formPayload() {
  return {
    name: $("profile-name").value, model: $("profile-model").value,
    api_base: $("profile-api-base").value,
    reasoning_effort: $("profile-reasoning").value,
    search_provider: $("profile-search").value,
    search_endpoint: $("profile-search-endpoint").value,
    llm_key: $("profile-key").value,
  };
}

async function saveProfile() {
  const payload = formPayload();
  const existing = state.profiles.find((item) => item.id === state.editing);
  const check = L.validateProfile({ ...payload, needs_key: !existing && !payload.llm_key });
  text($("profile-errors"), check.ok ? "" : Object.values(check.errors).join("; "));
  if (!check.ok) return false;

  setRequest({ type: "start", message: "saving..." });
  payload.llm_key = payload.llm_key || null;
  $("profile-key").value = "";  // cleared before the request returns
  try {
    const saved = state.editing
      ? await api("/api/settings/profiles/" + state.editing, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      : await api("/api/settings/profiles", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    state.editing = saved.id;
    setRequest({ type: "ok", message: "saved", rows: [] });
    await loadSettings();
    return true;
  } catch (error) {
    setRequest({ type: "error", message: "save failed: " + error.message });
    return false;
  }
}

async function testProfile() {
  if (!state.editing) {
    const saved = await saveProfile();
    if (!saved) { setRequest({ type: "error", message: "save the provider first" }); return; }
  }
  const started = Date.now();
  setRequest({ type: "start", message: "testing... 0s" });
  const ticker = setInterval(() => {
    const seconds = Math.round((Date.now() - started) / 1000);
    state.request = { ...state.request, message: "testing... " + seconds + "s" };
    text($("profile-status"), state.request.message);
  }, 1000);
  try {
    const data = await api("/api/settings/profiles/" + state.editing + "/test", { method: "POST" });
    const rows = data.results || [];
    setRequest({ type: "ok", message: rows.length + " path(s) checked", rows });
  } catch (error) {
    setRequest(error.code === "timeout"
      ? { type: "timeout" }
      : { type: "error", message: "test failed: " + error.message });
  } finally {
    clearInterval(ticker);
  }
}

async function activateProfile(profileId) {
  const id = typeof profileId === "string" && profileId ? profileId : state.editing;
  if (!id) {
    setRequest({ type: "error", message: "select a saved provider or use its activate action" });
    return;
  }
  const selected = state.profiles.find((profile) => profile.id === id);
  if (!selected) {
    setRequest({ type: "error", message: "select a saved provider first" });
    return;
  }
  editProfile(selected);
  setRequest({ type: "start", message: "restarting crew server..." });
  try {
    const result = await api("/api/settings/activate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: id }),
    });
    if (result.ok) {
      setRequest({ type: "ok", message: "crew server: up", rows: [] });
    } else {
      setRequest({
        type: "error",
        message: "profile was not activated; crew server did not start",
        rows: [],
      });
    }
    await loadSettings();
    await refreshHealth();
  } catch (error) {
    setRequest(error.code === "timeout"
      ? { type: "timeout" }
      : { type: "error", message: String(error.message) });
  }
}

async function deleteProfile() {
  if (!state.editing) { setRequest({ type: "error", message: "nothing selected" }); return; }
  if (!window.confirm("Delete this provider and its stored key?")) return;
  setRequest({ type: "start", message: "deleting..." });
  try {
    await api("/api/settings/profiles/" + state.editing, { method: "DELETE" });
    state.editing = "";
    setRequest({ type: "ok", message: "deleted", rows: [] });
    await loadSettings();
  } catch (error) {
    setRequest({ type: "error", message: "delete failed: " + error.message });
  }
}

async function saveAboutMe() {
  try {
    const saved = await api("/api/settings/about-me", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ about_me: $("about-me").value.slice(0, 800) }),
    });
    text($("about-status"), saved.about_me ? "saved" : "cleared");
  } catch (error) {
    text($("about-status"), "failed: " + error.message);
  }
}

/* ---------------------------------------------------------------- wiring */

$("composer-pattern").addEventListener("change", (event) => {
  setPattern(event.target.value);
});
document.querySelectorAll("#pattern-chips .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    setPattern(chip.dataset.pattern);
  });
});
setPattern(state.pattern, false);

document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => showView(item.dataset.view));
});

document.querySelectorAll("#run-tabs .tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    if (tab.dataset.tab === "answer") { showView("run"); return; }
    setTab(tab.dataset.tab);
  });
});

$("new-chat").addEventListener("click", newChat);
$("reconnect-run").addEventListener("click", reconnectRun);
$("exchange-details").addEventListener("click", () => showView("details"));
$("previous-run-dismiss").addEventListener("click", () => {
  state.previousRun = null;
  renderStatus();
});
$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  startRun();
});
$("question").addEventListener("input", autoGrow);
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    startRun();
  }
});

$("compare-all").addEventListener("click", compareAll);
$("examples-toggle").addEventListener("click", () => {
  const popover = $("examples-popover");
  const open = popover.classList.toggle("hidden");
  $("examples-toggle").setAttribute("aria-expanded", open ? "false" : "true");
});
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
$("sidebar-toggle").addEventListener("click", () => document.body.classList.toggle("sidebar-collapsed"));
$("history-refresh").addEventListener("click", loadHistory);
$("history-more").addEventListener("click", () => {
  state.historyLimit += HISTORY_LIMIT;
  renderHistory();
});
$("tools-refresh").addEventListener("click", loadTools);
$("profile-save").addEventListener("click", saveProfile);
$("profile-test").addEventListener("click", testProfile);
$("profile-activate").addEventListener("click", () => activateProfile());
$("profile-delete").addEventListener("click", deleteProfile);
$("about-save").addEventListener("click", saveAboutMe);
$("profile-preset").addEventListener("change", (event) => {
  const filled = L.presetFill(state.presets, event.target.value);
  if (filled.api_base) $("profile-api-base").value = filled.api_base;
  if (filled.model) $("profile-model").value = filled.model;
});
$("theme-toggle").addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
});
$("presentation-toggle").addEventListener("click", () => {
  applySize(document.documentElement.dataset.size !== "presentation");
});
$("banner").addEventListener("click", () => {
  state.noticeDismissed = true;
  banner("", false);
});

/* boot: theme, size, deep links */
try {
  applyTheme(localStorage.getItem("athena.theme") || "dark");
  applySize(localStorage.getItem("athena.presentation") === "1");
} catch (_) {
  applyTheme("dark");
  applySize(false);
}

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

if (boot.view.startsWith("replay=")) {
  const name = boot.view.slice(7);
  const at = boot.params.at === undefined ? 1 : Number(boot.params.at);
  openReplay(name, { at: Number.isFinite(at) ? at : 1, tab: bootTab });
} else if (["settings", "how", "documents"].includes(boot.view) || boot.view === "details") {
  showView(boot.view);
} else if (bootTab !== "answer") {
  showView("details");
  setTab(bootTab);
} else {
  showView("run");
}
setInterval(refreshHealth, 20000);
