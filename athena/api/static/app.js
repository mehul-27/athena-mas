/* Athena demo page. Rendering logic lives in logic.js; this file touches the DOM. */
"use strict";

const L = window.AthenaLogic;
const state = {
  runId: null, source: null, replaying: false, retries: 0,
  bundles: [], events: [], rawJson: false, runs: [],
};
const MAX_RETRIES = 5;

const $ = (id) => document.getElementById(id);
const text = (node, value) => { node.textContent = value == null ? "" : String(value); };

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.status;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* keep code */ }
    throw new Error(detail);
  }
  return response.json();
}

function banner(message, show) {
  const node = $("banner");
  text(node, message);
  node.classList.toggle("hidden", !show);
  node.dataset.dismissed = "false";
}

/* ---------- header ---------- */

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    const strip = L.statusStrip(health, state.replaying);
    text($("status-crew"), "crew server: " + strip.crew);
    text($("status-model"), "model: " + strip.model);
    text($("status-host"), "provider: " + strip.host);
    text($("status-mode"), "mode: " + strip.mode);
    if (!health.provider_configured && !state.replaying) {
      banner("No provider key configured - pick a recorded run to replay.", true);
    }
  } catch (error) {
    banner("API unreachable: " + error.message, true);
  }
}

/* ---------- ask panel ---------- */

async function loadQuestions() {
  const data = await api("/api/questions");
  const select = $("demo-question");
  data.demo.concat(data.eval.slice(0, 40)).forEach((item) => {
    const option = document.createElement("option");
    option.value = item.text;
    text(option, (item.id ? item.id + " - " : "") + item.text.slice(0, 90));
    select.appendChild(option);
  });
  select.addEventListener("change", () => { $("question").value = select.value; });
}

async function loadReplays() {
  const data = await api("/api/replay");
  state.bundles = data.bundles;
  const select = $("replay-select");
  select.replaceChildren(new Option("none", ""));
  data.bundles.forEach((bundle) => {
    const label = bundle.name + " (" + bundle.pattern + ", " + bundle.events + " events)"
      + (bundle.note ? " - " + bundle.note : "");
    select.appendChild(new Option(label, bundle.name));
  });
  select.addEventListener("change", () => { if (select.value) playReplay(select.value); });
  renderCompareFromBundles(data.bundles);
}

/* ---------- timeline ---------- */

function renderTimeline(events) {
  const rows = L.reduceTimeline(events);
  const body = $("timeline-body");
  body.replaceChildren();
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.className = row.status;
    [row.agent, row.framework.toUpperCase(), row.status,
     row.ms == null ? "" : row.ms + " ms",
     row.tokens_in + "/" + row.tokens_out,
     row.stop_reason || ""].forEach((value) => {
      const cell = document.createElement("td");
      text(cell, value);
      tr.appendChild(cell);
    });
    body.appendChild(tr);
  });
}

function addEventLine(kind, message, cssClass) {
  const item = document.createElement("li");
  item.className = cssClass || "";
  text(item, kind + " - " + message);
  $("event-log").appendChild(item);
  $("event-log").scrollTop = $("event-log").scrollHeight;
}

function logEvent(event) {
  if (event.kind === "a2a_call") {
    const extra = event.extra || {};
    addEventLine("A2A", event.agent + " " + (extra.request_bytes || 0) + "B out / "
      + (extra.response_bytes || 0) + "B in", "a2a");
  } else if (event.kind === "tool_call") {
    addEventLine("tool", event.summary || event.agent, "tool");
  } else if (event.kind === "blackboard_write") {
    addEventLine("blackboard", event.summary || event.agent);
  } else if (event.kind === "error") {
    addEventLine("error", (event.error_type || "error") + ": " + (event.error_message || ""), "error");
  }
}

function highlight(agent) {
  document.querySelectorAll(".node.active").forEach((node) => node.classList.remove("active"));
  const key = (agent || "").replace(/_researcher$/, "researcher");
  const node = $("n-" + key) || $("n-researcher");
  if (node) node.classList.add("active");
}

/* ---------- live stream (never used for replay) ---------- */

function openStream(runId) {
  closeStream();
  state.retries = 0;
  const source = new EventSource("/api/runs/" + runId + "/stream");
  state.source = source;
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    state.events.push(event);
    renderTimeline(state.events);
    highlight(event.agent);
    logEvent(event);
  };
  source.addEventListener("blackboard", () => loadSnapshot(runId));
  source.addEventListener("done", (message) => {
    const summary = JSON.parse(message.data).summary || {};
    addEventLine("done", "degraded=" + summary.degraded + " " + verdictOf(summary));
    closeStream();
    loadResult(runId);
  });
  source.onerror = () => {
    state.retries += 1;
    if (state.retries > MAX_RETRIES) {
      addEventLine("sse", "stream ended", "error");
      closeStream();
      loadResult(runId);
      return;
    }
    addEventLine("sse", "reconnecting (" + state.retries + "/" + MAX_RETRIES + ")");
  };
}

function closeStream() {
  if (state.source) { state.source.close(); state.source = null; }
}

function verdictOf(summary) {
  if (summary.approved === undefined) return "critic unknown";
  return L.verdictLabel(summary.approved, summary.severity);
}

/* ---------- runs ---------- */

async function startRun() {
  const question = $("question").value.trim();
  const pattern = $("pattern").value;
  if (!question) { banner("Type a question first.", true); return; }
  try {
    const data = await api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, pattern }),
    });
    state.runId = data.run_id;
    state.events = [];
    state.replaying = false;
    showRunId(data.run_id);
    $("event-log").replaceChildren();
    $("timeline-body").replaceChildren();
    openStream(data.run_id);
  } catch (error) {
    banner(String(error.message), true);
  }
}

function showRunId(runId) {
  text($("footer-run"), "run: " + runId);
  $("link-json").href = "/api/runs/" + runId;
  $("link-log").href = "/api/runs/" + runId + "/log";
}

async function loadResult(runId) {
  await loadSnapshot(runId);
  $("heatmap").src = "/api/runs/" + runId + "/heatmap.png?t=" + Date.now();
}

/* ---------- blackboard ---------- */

async function loadSnapshot(runId) {
  const snapshot = await api("/api/runs/" + runId);
  renderBlackboard(snapshot);
  if (snapshot.final_report) {
    renderReport(snapshot.final_report.value, snapshot.critique);
  }
}

function renderBlackboard(snapshot) {
  const host = $("blackboard-sections");
  host.replaceChildren();
  L.blackboardCards(snapshot).forEach((card) => {
    const box = document.createElement("div");
    box.className = "card";
    const title = document.createElement("h4");
    text(title, card.name + (card.version ? "  v" + card.version : ""));
    box.appendChild(title);
    if (card.verdict) {
      const badge = document.createElement("p");
      badge.className = "verdict";
      text(badge, card.verdict);
      box.appendChild(badge);
    }
    if (card.table) {
      const table = document.createElement("table");
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
  raw.className = state.rawJson ? "raw" : "raw hidden";
  text(raw, JSON.stringify(snapshot, null, 1).slice(0, 8000));
  host.appendChild(raw);
}

/* ---------- report ---------- */

function renderReport(report, critique) {
  const host = $("report");
  host.replaceChildren();

  const answer = document.createElement("p");
  answer.className = "answer";
  text(answer, report.answer || "(no answer)");
  host.appendChild(answer);

  const points = document.createElement("ul");
  (report.key_points || []).forEach((point) => {
    const item = document.createElement("li");
    text(item, point);
    points.appendChild(item);
  });
  host.appendChild(points);

  const rows = L.citationRows(report);
  if (rows.length) {
    const table = document.createElement("table");
    const head = document.createElement("tr");
    ["#", "source", "quote"].forEach((label) => {
      const th = document.createElement("th");
      text(th, label);
      head.appendChild(th);
    });
    table.appendChild(head);
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      [String(row.index), row.source_ref, row.quote].forEach((value) => {
        const cell = document.createElement("td");
        text(cell, value);
        tr.appendChild(cell);
      });
      table.appendChild(tr);
    });
    host.appendChild(table);
  }

  const verdict = critique ? L.verdictFromCritique(critique) : { approved: null, severity: null };
  const footer = document.createElement("p");
  footer.className = "verdict";
  text(footer, "confidence " + report.confidence + "  |  "
    + L.verdictLabel(verdict.approved, verdict.severity));
  host.appendChild(footer);
}

/* ---------- replay ---------- */

async function playReplay(name) {
  const bundle = await api("/api/replay/" + name);
  state.replaying = true;
  state.runId = bundle.run_id;
  state.events = [];
  closeStream();  // replay never opens SSE
  showRunId(bundle.run_id);
  text($("status-mode"), "mode: replay");
  banner("REPLAY - recorded run, not live.", true);
  $("event-log").replaceChildren();
  $("timeline-body").replaceChildren();

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
    state.events.push(event);
    logEvent(event);
  }
  renderTimeline(state.events);
  if (bundle.blackboard) {
    renderBlackboard({ ...bundle.blackboard, evidence: bundle.evidence || [] });
    const final = bundle.blackboard.final_report;
    if (final) renderReport(final.value, bundle.blackboard.critique);
  }
  if (bundle.heatmap_png) {
    $("heatmap").src = "data:image/png;base64," + bundle.heatmap_png;
  }
  addEventLine("replay", "finished " + name + " (" + bundle.events.length + " events)");
  await refreshHealth();
}

/* ---------- metrics and comparison ---------- */

function fillCompare(rows) {
  const body = $("compare-body");
  body.replaceChildren();
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    [row.pattern, Math.round(row.wall_s || 0), row.adk_tokens,
     (row.a2a_bytes_out || 0) + "/" + (row.a2a_bytes_in || 0), row.evidence_count,
     row.tool_calls, row.revision_rounds, row.confidence,
     L.verdictLabel(row.approved, row.severity), row.degraded ? "yes" : "no"].forEach((value) => {
      const cell = document.createElement("td");
      text(cell, value);
      tr.appendChild(cell);
    });
    body.appendChild(tr);
  });
}

function renderCompareFromBundles(bundles) {
  fillCompare(bundles.map((bundle) => ({
    pattern: bundle.name, wall_s: bundle.summary ? bundle.summary.wall_s : 0,
    adk_tokens: bundle.summary ? bundle.summary.adk_tokens : 0,
    a2a_bytes_out: bundle.summary ? bundle.summary.a2a_bytes_out : 0,
    a2a_bytes_in: bundle.summary ? bundle.summary.a2a_bytes_in : 0,
    evidence_count: bundle.summary ? bundle.summary.evidence_count : 0,
    tool_calls: bundle.summary ? bundle.summary.tool_calls : 0,
    revision_rounds: bundle.summary ? bundle.summary.revision_rounds : 0,
    confidence: bundle.summary ? bundle.summary.confidence : null,
    approved: bundle.approved, severity: null, degraded: bundle.degraded,
  })));
}

async function compareAll() {
  const question = $("question").value.trim();
  if (!question) { banner("Type a question first.", true); return; }
  state.runs = [];
  for (const pattern of ["sequential", "parallel", "hierarchical", "p2p"]) {
    const data = await api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, pattern }),
    });
    state.runs.push(data.run_id);
    await waitForRun(data.run_id);
  }
  const rows = await api("/api/compare?run=" + state.runs.join(","));
  fillCompare(rows.rows);
}

async function waitForRun(runId) {
  await new Promise((resolve) => {
    const source = new EventSource("/api/runs/" + runId + "/stream");
    source.addEventListener("done", () => { source.close(); resolve(); });
    source.onerror = () => { source.close(); resolve(); };
  });
}

/* ---------- history ---------- */

async function loadHistory() {
  const data = await api("/api/runs");
  const body = $("history-body");
  body.replaceChildren();
  (data.runs || []).forEach((run) => {
    const tr = document.createElement("tr");
    [run.question ? run.question.slice(0, 70) : "(unknown)",
     run.pattern, run.started_at, run.confidence,
     L.verdictLabel(run.approved, run.severity), run.degraded ? "yes" : "no"].forEach((value) => {
      const cell = document.createElement("td");
      text(cell, value);
      tr.appendChild(cell);
    });
    tr.className = "clickable";
    tr.addEventListener("click", () => openRun(run.run_id));
    body.appendChild(tr);
  });
}

async function openRun(runId) {
  state.replaying = true;  // a finished run is shown like a replay: no SSE
  state.runId = runId;
  showRunId(runId);
  const snapshot = await api("/api/runs/" + runId);
  renderBlackboard(snapshot);
  if (snapshot.final_report) renderReport(snapshot.final_report.value, snapshot.critique);
  $("heatmap").src = "/api/runs/" + runId + "/heatmap.png?t=" + Date.now();
  text($("status-mode"), "mode: replay");
}

/* ---------- tools ---------- */

async function loadTools() {
  const data = await api("/api/tools");
  const host = $("tools-list");
  host.replaceChildren();
  (data.servers || []).forEach((server) => {
    const card = document.createElement("div");
    card.className = "card";
    const title = document.createElement("h4");
    text(title, server.name);
    card.appendChild(title);
    const list = document.createElement("ul");
    (server.tools || []).forEach((tool) => {
      const item = document.createElement("li");
      text(item, tool.name + " - " + tool.description);
      list.appendChild(item);
    });
    card.appendChild(list);
    host.appendChild(card);
  });
}

/* ---------- wiring ---------- */

$("run-button").addEventListener("click", startRun);
$("compare-all").addEventListener("click", compareAll);
$("copy-markdown").addEventListener("click", () => {
  navigator.clipboard.writeText($("report").innerText || "")
    .then(() => banner("Markdown copied.", true), () => banner("Copy failed.", true));
});
$("toggle-raw").addEventListener("click", () => {
  state.rawJson = !state.rawJson;
  document.querySelectorAll(".raw").forEach((node) => {
    node.classList.toggle("hidden", !state.rawJson);
  });
});
$("banner").addEventListener("click", (event) => {
  event.currentTarget.classList.add("hidden");
});
$("tab-history").addEventListener("click", loadHistory);
$("tab-tools").addEventListener("click", loadTools);

refreshHealth();
loadQuestions().catch(() => {});
loadReplays().catch(() => {});
setInterval(refreshHealth, 20000);
