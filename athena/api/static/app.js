/* Athena demo page: vanilla JS, no dependencies, no external URLs. */
"use strict";

const state = { runId: null, source: null, replaying: false, runs: [] };

const $ = (id) => document.getElementById(id);
const text = (node, value) => { node.textContent = value == null ? "" : String(value); };

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error((await response.json()).detail || response.status);
  return response.json();
}

function banner(message, show) {
  const node = $("banner");
  text(node, message);
  node.classList.toggle("hidden", !show);
}

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    text($("status-crew"), "crew server: " + health.crew_server);
    text($("status-model"), "model: " + (health.model || "not set"));
    text($("status-host"), "provider: " + (health.api_host || "default"));
    text($("status-mode"), "mode: live");
    if (!health.provider_configured) {
      banner("No provider key configured - replay mode only.", true);
    } else if (health.crew_server === "down") {
      banner("CrewAI server is down. Start it with: python scripts/dev_up.py", true);
    }
  } catch (error) {
    banner("API unreachable: " + error.message, true);
  }
}

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
  const select = $("replay-select");
  data.bundles.forEach((bundle) => {
    const option = document.createElement("option");
    option.value = bundle.name;
    text(option, bundle.name + " (" + bundle.pattern + ", " + bundle.events + " events)");
    select.appendChild(option);
  });
  select.addEventListener("change", () => { if (select.value) playReplay(select.value); });
}

function addEventLine(kind, message, cssClass) {
  const item = document.createElement("li");
  item.className = cssClass || "";
  text(item, kind + " - " + message);
  $("event-log").appendChild(item);
  $("event-log").scrollTop = $("event-log").scrollHeight;
}

function upsertTimeline(event) {
  const body = $("timeline-body");
  const id = "row-" + event.agent;
  let row = document.getElementById(id);
  if (!row) {
    row = document.createElement("tr");
    row.id = id;
    for (let i = 0; i < 5; i += 1) row.appendChild(document.createElement("td"));
    body.appendChild(row);
  }
  const cells = row.children;
  text(cells[0], event.agent);
  text(cells[1], event.framework || "");
  text(cells[2], event.kind === "stage_end" ? (event.ok ? "done" : "failed") : "running");
  text(cells[3], event.duration_ms ? Math.round(event.duration_ms) : "");
  text(cells[4], (event.tokens_in || 0) + "/" + (event.tokens_out || 0));
  row.className = event.ok === false ? "failed" : "";
}

function highlight(agent, framework) {
  document.querySelectorAll(".node.active").forEach((node) => node.classList.remove("active"));
  const node = $("n-" + (agent || "").replace(/_researcher$/, "researcher"));
  if (node) node.classList.add("active");
  if (framework === "crewai") $("e-supervisor-crew").classList.add("pulse");
}

function handleEvent(event) {
  upsertTimeline(event);
  highlight(event.agent, event.framework);
  if (event.kind === "a2a_call") {
    const bytes = (event.extra && event.extra.request_bytes) || 0;
    const back = (event.extra && event.extra.response_bytes) || 0;
    addEventLine("A2A", event.agent + " " + bytes + "B out / " + back + "B in", "a2a");
  } else if (event.kind === "tool_call") {
    addEventLine("tool", event.summary || event.agent, "tool");
    document.getElementById("e-researcher-mcp").classList.add("pulse");
  } else if (event.kind === "blackboard_write") {
    addEventLine("blackboard", event.summary || event.agent);
    loadSnapshot(event.run_id);
  } else if (event.kind === "error") {
    addEventLine("error", event.error_type + ": " + (event.error_message || ""), "error");
  }
}

function openStream(runId) {
  closeStream();
  const source = new EventSource("/api/runs/" + runId + "/stream");
  state.source = source;
  source.onmessage = (message) => handleEvent(JSON.parse(message.data));
  source.addEventListener("blackboard", () => loadSnapshot(runId));
  source.addEventListener("done", (message) => {
    const summary = JSON.parse(message.data).summary || {};
    addEventLine("done", "degraded=" + summary.degraded + " verdict=" +
      (summary.approved === false ? "rejected" : summary.approved === true ? "approved" : "unknown"));
    closeStream();
    loadResult(runId);
  });
  source.onerror = () => addEventLine("sse", "reconnecting...");
}

function closeStream() {
  if (state.source) { state.source.close(); state.source = null; }
}

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
    text($("footer-run"), "run: " + data.run_id);
    $("link-json").href = "/api/runs/" + data.run_id;
    $("link-log").href = "/api/runs/" + data.run_id + "/log";
    $("event-log").replaceChildren();
    $("timeline-body").replaceChildren();
    openStream(data.run_id);
  } catch (error) {
    banner(String(error.message), true);
  }
}

async function loadSnapshot(runId) {
  const snapshot = await api("/api/runs/" + runId);
  renderBlackboard(snapshot);
  if (snapshot.final_report) renderReport(snapshot.final_report.value, snapshot.critique);
}

function renderBlackboard(snapshot) {
  const host = $("blackboard-sections");
  host.replaceChildren();
  const sections = ["task", "plan", "analysis", "draft", "critique", "final_report"];
  sections.forEach((name) => {
    const section = snapshot[name];
    if (!section) return;
    const card = document.createElement("div");
    card.className = "card";
    const title = document.createElement("h4");
    text(title, name);
    const caption = document.createElement("div");
    caption.className = "caption";
    text(caption, "version " + section.version);
    const body = document.createElement("pre");
    text(body, JSON.stringify(section.value, null, 1).slice(0, 1200));
    card.append(title, caption, body);
    host.appendChild(card);
  });
  if (snapshot.evidence && snapshot.evidence.length) {
    const card = document.createElement("div");
    card.className = "card";
    const title = document.createElement("h4");
    text(title, "evidence (" + snapshot.evidence.length + ")");
    const body = document.createElement("pre");
    text(body, snapshot.evidence.map((item) =>
      item.retrieved_by + " | " + item.source_ref + " | " + item.score + " | " +
      item.text.slice(0, 120)).join("\n"));
    card.append(title, body);
    host.appendChild(card);
  }
}

function renderReport(report, critique) {
  const host = $("report");
  host.replaceChildren();
  const answer = document.createElement("p");
  text(answer, report.answer || "(no answer)");
  host.appendChild(answer);

  const points = document.createElement("ul");
  (report.key_points || []).forEach((point) => {
    const item = document.createElement("li");
    text(item, point);
    points.appendChild(item);
  });
  host.appendChild(points);

  const table = document.createElement("table");
  const head = document.createElement("tr");
  ["source", "quote"].forEach((label) => {
    const th = document.createElement("th");
    text(th, label);
    head.appendChild(th);
  });
  table.appendChild(head);
  (report.citations || []).forEach((citation) => {
    const row = document.createElement("tr");
    [citation.source_ref, citation.quote].forEach((value) => {
      const cell = document.createElement("td");
      text(cell, value);
      row.appendChild(cell);
    });
    table.appendChild(row);
  });
  host.appendChild(table);

  const verdict = document.createElement("p");
  const state_ = critique && critique.value
    ? (critique.value.approved ? "approved" : "rejected") + " (" + critique.value.severity + ")"
    : "no critique";
  text(verdict, "confidence " + report.confidence + " | critic " + verdict);
  host.appendChild(verdict);
}

async function loadResult(runId) {
  await loadSnapshot(runId);
  $("heatmap").src = "/api/runs/" + runId + "/heatmap.png?t=" + Date.now();
  const metrics = await api("/api/runs/" + runId + "/metrics");
  if ((metrics.rows || []).length) {
    addEventLine("metrics", metrics.rows.length + " agents measured");
  }
}

async function playReplay(name) {
  const bundle = await api("/api/replay/" + name);
  state.replaying = true;
  text($("status-mode"), "mode: REPLAY");
  banner("REPLAY: recorded run, not live.", true);
  $("event-log").replaceChildren();
  $("timeline-body").replaceChildren();
  const speed = Number($("replay-speed").value);
  const first = bundle.events.length ? Date.parse(bundle.events[0].ts) : 0;
  let before = 0;
  for (const event of bundle.events) {
    if (speed > 0) {
      const when = Date.parse(event.ts) - first;
      const wait = Math.max(0, (when - before) / speed);
      before = when;
      await new Promise((resolve) => setTimeout(resolve, Math.min(wait, 800)));
    }
    handleEvent(event);
  }
  if (bundle.blackboard) renderBlackboard(bundle.blackboard);
  const final = bundle.blackboard && bundle.blackboard.final_report;
  if (final) renderReport(final.value, bundle.blackboard.critique);
  addEventLine("replay", "finished " + name);
}

async function compareAll() {
  const question = $("question").value.trim();
  if (!question) { banner("Type a question first.", true); return; }
  $("compare-body").replaceChildren();
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
  renderCompare(rows.rows);
}

async function waitForRun(runId) {
  await new Promise((resolve) => {
    const source = new EventSource("/api/runs/" + runId + "/stream");
    source.addEventListener("done", () => { source.close(); resolve(); });
    source.onerror = () => { source.close(); resolve(); };
  });
}

function renderCompare(rows) {
  const body = $("compare-body");
  body.replaceChildren();
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    [row.pattern, Math.round(row.wall_s || 0), row.adk_tokens,
     (row.a2a_bytes_out || 0) + "/" + (row.a2a_bytes_in || 0), row.evidence_count,
     row.tool_calls, row.revision_rounds, row.confidence,
     row.approved === true ? "approved" : row.approved === false ? "rejected" : "-",
     row.degraded ? "yes" : "no"].forEach((value) => {
      const cell = document.createElement("td");
      text(cell, value);
      tr.appendChild(cell);
    });
    body.appendChild(tr);
  });
  text($("p2p-note"), "p2p carries the context in A2A messages, so its byte count is " +
    "much higher than the blackboard patterns.");
}

function copyMarkdown() {
  const report = $("report");
  navigator.clipboard.writeText(report.innerText || "").then(
    () => banner("Markdown copied.", true), () => banner("Copy failed.", true));
}

$("run-button").addEventListener("click", startRun);
$("compare-all").addEventListener("click", compareAll);
$("copy-markdown").addEventListener("click", copyMarkdown);

refreshHealth();
loadQuestions().catch(() => {});
loadReplays().catch(() => {});
setInterval(refreshHealth, 15000);
