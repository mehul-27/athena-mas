/* Athena UI wiring. Pure logic lives in logic.js; the SVG in diagram.js.
 * All backend text is untrusted: everything is built with textContent.
 */
"use strict";

const L = window.AthenaLogic;
const D = window.AthenaDiagram;

const TIMEOUT_MS = 150000;
const MAX_RETRIES = 5;

const state = {
  view: "run", pattern: "sequential", runId: null, events: [],
  source: null, retries: 0, replaying: false, bundles: [], presets: [],
  profiles: [], active: "", editing: "", rawJson: false, busy: false,
  request: { status: "idle", message: "", rows: [] },
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
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
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

function banner(message, show) {
  const node = $("banner");
  text(node, message);
  node.classList.toggle("hidden", !show);
}

/* ---------------------------------------------------------------- chrome */

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
  ["run", "how", "documents", "settings"].forEach((view) => {
    const node = $("view-" + view);
    if (node) node.classList.toggle("hidden", view !== name);
  });
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.view === name);
  });
  if (name === "how") { renderHowDiagram(); loadTools(); }
  if (name === "settings") loadSettings();
  if (name === "run") renderLiveMap();
  try { history.replaceState(null, "", "#" + name); } catch (_) { /* ignore */ }
}

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    const pill = L.statusPill(health, state.replaying);
    text($("status-crew"), "crew: " + pill.crew);
    text($("status-mode"), "mode: " + pill.mode);
    text($("status-model"), "model: " + pill.model);
    text($("status-host"), "provider: " + pill.host);
    $("status-crew-dot").className = "dot " + (pill.crew === "up" ? "up" : "down");
    if (!health.provider_configured && !state.replaying) {
      banner("No provider configured. Open Settings to add one, or replay a recording.", true);
    }
  } catch (error) {
    const dot = $("status-crew-dot");
    if (dot) dot.className = "dot down";
    banner("API unreachable: " + error.message, true);
  }
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

async function loadQuestions() {
  const data = await api("/api/questions");
  const select = $("demo-question");
  data.demo.concat((data.eval || []).slice(0, 40)).forEach((item) => {
    const option = document.createElement("option");
    option.value = item.text;
    text(option, (item.id ? item.id + " - " : "") + String(item.text).slice(0, 90));
    select.appendChild(option);
  });
  select.addEventListener("change", () => { $("question").value = select.value; });
}

async function loadReplays() {
  const data = await api("/api/replay");
  state.bundles = data.bundles || [];
  const host = $("replay-list");
  host.replaceChildren();
  if (!state.bundles.length) emptyItem(host, "no recordings");
  state.bundles.forEach((bundle) => {
    host.appendChild(sidebarItem(bundle.name, bundle.pattern + (bundle.note ? " · note" : ""),
      () => openReplay(bundle.name)));
  });
  const select = $("replay-select");
  select.replaceChildren(new Option("replay a recording...", ""));
  state.bundles.forEach((bundle) => select.appendChild(new Option(bundle.name, bundle.name)));
  fillCompare(state.bundles.map((bundle) => ({
    name: bundle.name, pattern: bundle.pattern, degraded: bundle.degraded,
    approved: bundle.approved, summary: bundle.summary || {},
  })));
}

async function loadHistory() {
  const host = $("history-list");
  try {
    const data = await api("/api/runs");
    const runs = (data.runs || []).slice(-25).reverse();
    host.replaceChildren();
    if (!runs.length) { emptyItem(host, "no runs yet"); return; }
    runs.forEach((run) => {
      host.appendChild(sidebarItem(
        String(run.question || "(no question)").slice(0, 60),
        run.pattern + " · " + L.verdictLabel(run.approved, run.severity),
        () => openRun(run.run_id)));
    });
  } catch (error) {
    host.replaceChildren();
    emptyItem(host, "history unavailable");
  }
}

/* ---------------------------------------------------------------- run */

function pattern() {
  const chip = document.querySelector("#pattern-chips .chip.selected");
  return chip ? chip.dataset.pattern : "sequential";
}

function setBusy(busy) {
  state.busy = busy;
  const button = $("run-button");
  button.disabled = busy;
  text(button, busy ? "running..." : "Run");
}

async function startRun() {
  const question = $("question").value.trim();
  if (!question) { banner("Type a question first.", true); return; }
  state.pattern = pattern();
  try {
    setBusy(true);
    const data = await api("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, pattern: state.pattern }),
    });
    beginRun(data.run_id, false);
  } catch (error) {
    setBusy(false);
    banner("Could not start: " + error.message, true);
  }
}

function beginRun(runId, replaying) {
  closeStream();
  state.runId = runId;
  state.replaying = replaying;
  state.events = [];
  state.retries = 0;
  $("replay-badge").classList.toggle("hidden", !replaying);
  text($("run-hint"), "run id: " + runId);
  $("link-json").href = "/api/runs/" + runId;
  $("link-log").href = "/api/runs/" + runId + "/log";
  $("event-log").replaceChildren();
  $("timeline-body").replaceChildren();
  $("result-card").classList.add("hidden");
  renderLiveMap();
  if (!replaying) openStream(runId);
}

function openStream(runId) {
  closeStream();
  state.retries = 0;
  const source = new EventSource("/api/runs/" + runId + "/stream");
  state.source = source;
  source.onmessage = (message) => addEvent(JSON.parse(message.data));
  source.addEventListener("blackboard", () => loadSnapshot(runId));
  source.addEventListener("done", (message) => {
    const summary = (JSON.parse(message.data).summary) || {};
    addLog("done", "degraded=" + summary.degraded + " " + L.verdictLabel(summary.approved, summary.severity));
    closeStream();
    setBusy(false);
    finishRun(runId);
  });
  source.onerror = () => {
    state.retries += 1;
    if (state.retries > MAX_RETRIES) {
      addLog("stream", "stream ended", "error");
      closeStream();
      setBusy(false);
      finishRun(runId);
      return;
    }
    addLog("stream", "reconnecting (" + state.retries + "/" + MAX_RETRIES + ")");
  };
}

function closeStream() {
  if (state.source) { state.source.close(); state.source = null; }
}

function addEvent(event) {
  state.events.push(event);
  renderTimeline();
  renderLiveMap();
  logEvent(event);
}

function addLog(kind, message, cssClass) {
  const item = document.createElement("li");
  item.className = cssClass || "";
  text(item, kind + " · " + message);
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
    addLog("stage", event.agent + " " + L.duration(event.duration_ms) + " · "
      + L.tokens(event.tokens_in, event.tokens_out));
  }
}

function renderTimeline() {
  const body = $("timeline-body");
  body.replaceChildren();
  const rows = L.reduceTimeline(state.events);
  rows.forEach((row) => {
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
  const done = rows.filter((row) => row.status === "done" || row.status === "failed").length;
  text($("activity-summary"), rows.length ? done + "/" + rows.length + " agents finished" : "");
}

function renderLiveMap() {
  const spec = L.diagramFor(state.pattern);
  const view = L.diagramState(state.events, state.pattern);
  D.render($("live-map"), spec, view, { compact: true });
}

async function finishRun(runId) {
  await loadSnapshot(runId);
  $("heatmap").src = "/api/runs/" + runId + "/heatmap.png?t=" + Date.now();
  await loadHistory();
}

/* ---------------------------------------------------------------- replay */

async function openReplay(name) {
  try {
    const bundle = await api("/api/replay/" + name);
    state.pattern = (bundle.summary && bundle.summary.pattern) || state.pattern;
    beginRun(bundle.run_id, true);
    const speed = Number($("replay-speed").value);
    const first = bundle.events.length ? Date.parse(bundle.events[0].ts) : 0;
    let previous = 0;
    for (const event of bundle.events) {
      if (speed > 0) {
        const when = Date.parse(event.ts) - first;
        const wait = Math.max(0, (when - previous) / speed);
        previous = when;
        await new Promise((resolve) => setTimeout(resolve, Math.min(wait, 600)));
      }
      addEvent(event);
    }
    if (bundle.blackboard) {
      renderBlackboard({ ...bundle.blackboard, evidence: bundle.evidence || [] });
      const final = bundle.blackboard.final_report;
      if (final) renderReport(final.value, bundle.blackboard.critique);
    }
    if (bundle.heatmap_png) $("heatmap").src = "data:image/png;base64," + bundle.heatmap_png;
    addLog("replay", "finished " + name + " (" + bundle.events.length + " events)");
    await refreshHealth();
  } catch (error) {
    banner("Replay failed: " + error.message, true);
  }
}

async function openRun(runId) {
  beginRun(runId, true);
  try {
    const snapshot = await api("/api/runs/" + runId);
    renderBlackboard(snapshot);
    if (snapshot.final_report) renderReport(snapshot.final_report.value, snapshot.critique);
    $("heatmap").src = "/api/runs/" + runId + "/heatmap.png?t=" + Date.now();
    state.pattern = (snapshot.task && snapshot.task.value.pattern) || state.pattern;
    renderLiveMap();
  } catch (error) {
    banner("Could not open run: " + error.message, true);
  }
}

/* ---------------------------------------------------------------- snapshot */

async function loadSnapshot(runId) {
  const snapshot = await api("/api/runs/" + runId);
  renderBlackboard(snapshot);
  if (snapshot.final_report) renderReport(snapshot.final_report.value, snapshot.critique);
}

function renderBlackboard(snapshot) {
  const host = $("blackboard-sections");
  host.replaceChildren();
  L.blackboardCards(snapshot).forEach((card) => {
    const box = document.createElement("div");
    box.className = "board-card";
    const title = document.createElement("h4");
    text(title, card.name + (card.version ? " · v" + card.version : ""));
    box.appendChild(title);
    if (card.verdict) {
      const verdict = document.createElement("p");
      verdict.className = "verdict";
      text(verdict, card.verdict);
      box.appendChild(verdict);
    }
    if (card.table) {
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
      box.appendChild(table);
    } else {
      const body = document.createElement("pre");
      text(body, (card.lines || []).join("\n"));
      box.appendChild(body);
    }
    host.appendChild(box);
  });
  const raw = document.createElement("pre");
  raw.className = "raw" + (state.rawJson ? "" : " hidden");
  text(raw, JSON.stringify(snapshot, null, 1).slice(0, 12000));
  host.appendChild(raw);
}

function renderReport(report, critique) {
  $("result-card").classList.remove("hidden");
  text($("report-answer"), report.answer || "(no answer)");

  const points = $("report-points");
  points.replaceChildren();
  (report.key_points || []).forEach((point) => {
    const item = document.createElement("li");
    text(item, point);
    points.appendChild(item);
  });

  const verdict = critique ? L.verdictFromCritique(critique) : { approved: null, severity: null };
  const meta = $("report-meta");
  meta.replaceChildren();
  [["confidence " + report.confidence, ""],
   [L.verdictLabel(verdict.approved, verdict.severity), verdict.approved === false ? "bad" : "ok"],
   ["tools: " + ((report.tools_used || []).join(", ") || "none"), ""],
   ["sources " + ((report.citations || []).length), ""]].forEach(([label, tone]) => {
    const pill = document.createElement("span");
    pill.className = "pill " + tone;
    text(pill, label);
    meta.appendChild(pill);
  });

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
    text(item, row.source_ref + " - " + row.quote);
    list.appendChild(item);
  });
}

/* ---------------------------------------------------------------- metrics */

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
  if (!question) { banner("Type a question first.", true); return; }
  const ids = [];
  try {
    setBusy(true);
    for (const name of ["sequential", "parallel", "hierarchical", "p2p"]) {
      const data = await api("/api/runs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, pattern: name }),
      });
      ids.push(data.run_id);
      await new Promise((resolve) => {
        const source = new EventSource("/api/runs/" + data.run_id + "/stream");
        source.addEventListener("done", () => { source.close(); resolve(); });
        source.onerror = () => { source.close(); resolve(); };
      });
    }
    const rows = await api("/api/compare?run=" + ids.join(","));
    fillCompare((rows.rows || []).map((row) => ({ ...row, name: row.pattern, summary: row })));
  } catch (error) {
    banner("Compare failed: " + error.message, true);
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
    if (!state.active && state.profiles.length === 0) {
      banner("Add a provider in Settings to run live.", true);
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
  setRequest({ type: "start", message: "calling the provider once per path..." });
  try {
    const data = await api("/api/settings/profiles/" + state.editing + "/test", { method: "POST" });
    const rows = data.results || [];
    setRequest({ type: "ok", message: rows.length + " path(s) checked", rows });
  } catch (error) {
    setRequest(error.code === "timeout"
      ? { type: "timeout" }
      : { type: "error", message: "test failed: " + error.message });
  }
}

async function activateProfile() {
  if (!state.editing) {
    const saved = await saveProfile();
    if (!saved) { setRequest({ type: "error", message: "save the provider first" }); return; }
  }
  setRequest({ type: "start", message: "restarting crew server..." });
  try {
    const result = await api("/api/settings/activate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: state.editing }),
    });
    setRequest({ type: "ok", message: result.ok ? "crew server: up" : "crew server: down", rows: [] });
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

document.querySelectorAll("#pattern-chips .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll("#pattern-chips .chip").forEach((other) => other.classList.remove("selected"));
    chip.classList.add("selected");
    state.pattern = chip.dataset.pattern;
    renderLiveMap();
  });
});

document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => showView(item.dataset.view));
});

$("run-button").addEventListener("click", startRun);
$("compare-all").addEventListener("click", compareAll);
$("replay-select").addEventListener("change", (event) => {
  if (event.target.value) openReplay(event.target.value);
});
$("copy-markdown").addEventListener("click", () => {
  const body = $("report-answer").textContent + "\n\n"
    + Array.from($("report-points").children).map((item) => "- " + item.textContent).join("\n");
  navigator.clipboard.writeText(body)
    .then(() => banner("Markdown copied.", true), () => banner("Copy failed.", true));
});
$("toggle-raw").addEventListener("click", () => {
  state.rawJson = !state.rawJson;
  document.querySelectorAll(".raw").forEach((node) => node.classList.toggle("hidden", !state.rawJson));
});
$("activity-toggle").addEventListener("click", () => {
  const hidden = $("activity-body").classList.toggle("hidden");
  text($("activity-toggle"), hidden ? "expand" : "collapse");
});
$("sidebar-toggle").addEventListener("click", () => document.body.classList.toggle("sidebar-collapsed"));
$("history-refresh").addEventListener("click", loadHistory);
$("tools-refresh").addEventListener("click", loadTools);
$("profile-save").addEventListener("click", saveProfile);
$("profile-test").addEventListener("click", testProfile);
$("profile-activate").addEventListener("click", activateProfile);
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
$("banner").addEventListener("click", (event) => event.currentTarget.classList.add("hidden"));
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); startRun(); }
});

/* boot */
try {
  applyTheme(localStorage.getItem("athena.theme") || "dark");
  applySize(localStorage.getItem("athena.presentation") === "1");
} catch (_) {
  applyTheme("dark");
  applySize(false);
}

const hash = (window.location.hash || "").replace("#", "");
refreshHealth();
loadQuestions().catch(() => {});
loadHistory();
loadReplays().then(() => {
  if (hash.startsWith("replay=")) openReplay(hash.slice(7));
  else if (hash === "settings" || hash === "how" || hash === "documents") showView(hash);
  else showView("run");
}).catch(() => showView("run"));
renderLiveMap();
setInterval(refreshHealth, 20000);
