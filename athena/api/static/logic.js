/* Pure view-model helpers for the demo UI. No DOM access; tested with node.
 * Loaded as a classic script (globalThis.AthenaLogic) and by tests
 * (`module.exports` when `module` exists).
 */
"use strict";

(function (root) {
  const FRAMEWORK = {
    planner: "adk", researcher: "adk", rag_researcher: "adk",
    web_researcher: "adk", calc_researcher: "adk", supervisor: "adk",
    analyst: "crewai", writer: "crewai", critic: "crewai",
  };

  const PATTERNS = Object.freeze(["sequential", "parallel", "hierarchical", "p2p"]);
  const DEFAULT_PATTERN = "hierarchical";

  function patternValue(value) {
    return PATTERNS.includes(value) ? value : DEFAULT_PATTERN;
  }

  function validRunId(value) {
    return typeof value === "string" && value.trim().length > 0;
  }

  /* ---------------------------------------------------------------- metrics */

  function metricValue(value) {
    return (typeof value === "number" && Number.isFinite(value)) ? String(value) : "\u2014";
  }

  function metricRows(rows) {
    if (!Array.isArray(rows)) return [];
    return rows.filter((row) => row && typeof row === "object").map((row) => ({
      agent: typeof row.agent === "string" && row.agent ? row.agent : "(unknown)",
      framework: typeof row.framework === "string" && row.framework ? row.framework : "\u2014",
      stage: (typeof row.stage_ms === "number" && Number.isFinite(row.stage_ms))
        ? duration(row.stage_ms) : "\u2014",
      models: metricValue(row.model_calls),
      tokens_in: metricValue(row.tokens_in),
      tokens_out: metricValue(row.tokens_out),
      reasoning: metricValue(row.reasoning_tokens),
      tools: metricValue(row.tool_calls),
      errors: metricValue(row.errors),
      a2a: metricValue(row.a2a_bytes),
      evidence: metricValue(row.evidence_items),
      estimated: metricValue(row.estimated_calls),
    }));
  }

  const frameworkOf = (event) => event.framework || FRAMEWORK[event.agent] || "adk";

  /* ---------------------------------------------------------------- timeline */

  function reduceTimeline(events) {
    const rows = new Map();
    const ensure = (agent) => {
      if (!rows.has(agent)) {
        rows.set(agent, {
          agent, framework: "adk", status: "waiting", ms: null,
          model_in: 0, model_out: 0, stage_in: 0, stage_out: 0,
          reasoning: 0, stop_reason: "",
          tokens_known: false, has_model_call: false,
        });
      }
      return rows.get(agent);
    };
    (events || []).forEach((event) => {
      const row = ensure(event.agent || "?");
      row.framework = frameworkOf(event);
      if (event.kind === "stage_end") {
        row.status = event.ok === false ? "failed" : "done";
        if (event.duration_ms != null) row.ms = Math.round(event.duration_ms);
        if (event.tokens_in != null || event.tokens_out != null) {
          row.tokens_known = true;
          row.stage_in = event.tokens_in || 0;
          row.stage_out = event.tokens_out || 0;
        }
        if (event.extra && event.extra.stop_reason) row.stop_reason = event.extra.stop_reason;
      } else if (event.kind === "model_call") {
        row.has_model_call = true;
        if (event.tokens_in != null || event.tokens_out != null) row.tokens_known = true;
        row.model_in += event.tokens_in || 0;
        row.model_out += event.tokens_out || 0;
        if (event.extra && event.extra.reasoning_tokens) row.reasoning += event.extra.reasoning_tokens;
        if (row.status === "waiting") row.status = "running";
      } else if (event.kind === "stage_start" && row.status !== "done") {
        row.status = "running";
      } else if (event.kind === "error") {
        row.status = row.status === "done" ? "done" : "failed";
      }
    });
    return Array.from(rows.values()).map((row) => ({
      agent: row.agent, framework: row.framework, status: row.status, ms: row.ms,
      tokens_in: row.stage_in || row.model_in, tokens_out: row.stage_out || row.model_out,
      tokens_known: row.tokens_known,
      reasoning: row.reasoning, stop_reason: row.stop_reason,
    }));
  }

  function a2aBytes(events) {
    return (events || []).reduce((total, event) => {
      if (event.kind !== "a2a_call") return total;
      const extra = event.extra || {};
      return { out: total.out + (extra.request_bytes || 0),
               in: total.in + (extra.response_bytes || 0) };
    }, { out: 0, in: 0 });
  }

  /* ------------------------------------------------------------- formatting */

  function duration(ms) {
    if (ms == null) return "";
    return ms < 1000 ? Math.round(ms) + " ms" : (ms / 1000).toFixed(1) + " s";
  }

  function bytes(total) {
    const value = Number(total) || 0;
    return value < 1024 ? value + " B" : (value / 1024).toFixed(1) + " kB";
  }

  function tokens(inCount, outCount, known) {
    if (known === false) return "tokens unavailable";
    return (inCount || 0) + " in / " + (outCount || 0) + " out";
  }

  const VERDICT_WORD = { true: "approved", false: "rejected" };

  function verdictLabel(approved, severity) {
    if (approved === null || approved === undefined) return "critic unknown";
    const word = VERDICT_WORD[String(approved)];
    return severity ? "critic " + word + " (" + severity + ")" : "critic " + word;
  }

  function verdictFromCritique(critique) {
    if (!critique || !critique.value) return { approved: null, severity: null };
    const value = critique.value || critique;
    return { approved: Boolean(value.approved), severity: value.severity || null };
  }

  function critiqueDiagnostic(critique) {
    const value = critique && critique.value ? critique.value : critique;
    if (!value) {
      return {
        status: "missing",
        label: "No critique recorded",
        reason: "The critic did not produce a critique; this is not an approval.",
        issues: [],
      };
    }
    const issues = (Array.isArray(value.issues) ? value.issues : [])
      .map((issue) => String(issue)).filter(Boolean);
    const status = value.approved === true ? "approved"
      : value.approved === false ? "rejected" : "unknown";
    const label = status === "unknown"
      ? "Critic verdict unavailable"
      : verdictLabel(value.approved, value.severity);
    return {
      status,
      label,
      reason: issues.length ? issues[0]
        : status === "approved" ? "No issues recorded." : "No rejection reason was recorded.",
      issues,
    };
  }

  function citationRows(report) {
    return ((report && report.citations) || []).map((citation, index) => ({
      index: index + 1,
      source_ref: citation.source_ref || "(no source)",
      quote: citation.quote || citation.text || "(quote not recorded)",
    }));
  }

  /* Short label + tooltip: "crew up - live - qwen3.8 - gateway". */
  function statusPill(health, replaying) {
    const full = statusPillFull(health, replaying);
    const model = String(full.model || "").replace(/^.*\//, "").slice(0, 18);
    const parts = ["crew " + full.crew, full.mode];
    if (full.mode === "live") {
      if (model) parts.push(model);
      if (full.host && full.host !== "default") parts.push(full.host);
    }
    return {
      ...full,
      short: parts.join(" - "),
      tooltip: "crew server: " + full.crew + " | mode: " + full.mode
        + " | model: " + full.model + " | provider: " + full.host,
    };
  }

  function statusPillFull(health, replaying) {
    const mode = replaying || !health || !health.provider_configured ? "replay" : "live";
    const active = (health && health.active_profile) || null;
    return {
      crew: (health && health.crew_server) || "unknown",
      model: mode === "replay" ? "n/a (replay)"
        : (health.model || (active && active.model) || "not set"),
      host: mode === "replay" ? "n/a (replay)"
        : (health.api_host || (active && active.api_host) || "default"),
      mode,
    };
  }

  /* ------------------------------------------------------------- blackboard */

  function blackboardCards(snapshot) {
    const cards = [];
    const value = (name) => (snapshot[name] ? snapshot[name].value : null);
    const version = (name) => (snapshot[name] ? snapshot[name].version : null);

    const task = value("task");
    if (task) cards.push({ name: "task", version: version("task"),
      lines: [task.question || "", "pattern: " + (task.pattern || "?")] });

    const plan = value("plan");
    if (plan) {
      const lines = (plan.steps || []).map((step, i) =>
        (i + 1) + ". " + (step.description || "") + " -> " + (step.assigned_to || "?"));
      (plan.blocked || []).forEach((item) => lines.push("blocked: " + item));
      cards.push({ name: "plan", version: version("plan"), lines });
    }

    if (snapshot.evidence && snapshot.evidence.length) {
      cards.push({ name: "evidence", version: null, table: true,
        columns: ["author", "source", "score", "snippet"],
        rows: snapshot.evidence.map((item) => [
          item.retrieved_by || "?", item.source_ref || "?", String(item.score ?? ""),
          String(item.text || "").slice(0, 200)]) });
    }

    const analysis = value("analysis");
    if (analysis) cards.push({ name: "analysis", version: version("analysis"),
      lines: (analysis.key_findings || []).map((finding) => "- " + finding)
        .concat((analysis.gaps || []).map((gap) => "gap: " + gap)) });

    const draft = value("draft");
    if (draft) cards.push({ name: "draft", version: version("draft"),
      lines: [String(draft.text || "").slice(0, 600)] });

    const critique = value("critique");
    if (critique) cards.push({ name: "critique", version: version("critique"),
      verdict: verdictLabel(critique.approved, critique.severity),
      lines: (critique.issues || []).map((issue) => "- " + issue) });

    const report = value("final_report");
    if (report) cards.push({ name: "final_report", version: version("final_report"),
      lines: [(report.answer || "").slice(0, 600)] });

    return cards;
  }

  /* --------------------------------------------------------------- settings */

  function validateProfile(form) {
    const errors = {};
    const name = String(form.name || "").trim();
    const model = String(form.model || "").trim();
    const base = String(form.api_base || "").trim();

    if (!name) errors.name = "name is required";
    if (!model) errors.model = "model string is required";
    if (form.needs_key && !String(form.llm_key || "").trim()) {
      errors.llm_key = "an API key is required for a new provider";
    }
    if (base) {
      if (!/^https?:\/\//i.test(base)) errors.api_base = "base URL must start with https://";
      else if (!/^https:/i.test(base) && !/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])/i.test(base)) {
        errors.api_base = "http is only allowed for localhost";
      }
      if (/^https?:\/\/[^/]*@/i.test(base)) errors.api_base = "the URL must not contain credentials";
    }
    if (form.search_provider === "searxng" && !String(form.search_endpoint || "").trim()) {
      errors.search_endpoint = "SearXNG needs an endpoint";
    }
    return { ok: Object.keys(errors).length === 0, errors };
  }

  function presetFill(presets, presetId) {
    const preset = (presets || []).find((item) => item.id === presetId);
    if (!preset) return { api_base: "", model: "" };
    return { api_base: preset.api_base || "", model: preset.model || "" };
  }

  /* One request at a time: idle -> running -> ok | error | timeout. */
  function requestState(state, action) {
    const current = state || { status: "idle", message: "", rows: [] };
    switch (action.type) {
      case "start":
        return { status: "running", message: action.message || "working...", rows: [] };
      case "ok":
        return { status: "ok",
                 message: action.message || ((action.rows || []).length + " path(s) checked"),
                 rows: action.rows || [] };
      case "error":
        return { status: "error", message: action.message || "request failed", rows: [] };
      case "timeout":
        return { status: "timeout",
                 message: "taking too long; check the crew server", rows: [] };
      case "reset":
        return { status: "idle", message: "", rows: [] };
      default:
        return current;
    }
  }

  function isBusy(state) {
    return (state || {}).status === "running";
  }

  /* ---------------------------------------------------------------- diagram */

  const NODES = ["user", "supervisor", "planner", "rag_researcher", "web_researcher",
                 "calc_researcher", "analyst", "writer", "critic", "blackboard",
                 "rag_tool", "web_tool", "calc_tool"];

  const PATTERN_EDGES = {
    sequential: [
      ["user", "supervisor", ""], ["supervisor", "planner", ""],
      ["planner", "rag_researcher", ""], ["planner", "blackboard", "read/write"],
      ["rag_researcher", "blackboard", "read/write"], ["supervisor", "analyst", "A2A"],
      ["analyst", "blackboard", "read/write"], ["supervisor", "writer", "A2A"],
      ["writer", "blackboard", "read/write"], ["supervisor", "critic", "A2A"],
      ["rag_researcher", "rag_tool", "MCP"],
    ],
    parallel: [
      ["user", "supervisor", ""], ["supervisor", "planner", ""],
      ["planner", "rag_researcher", ""], ["planner", "web_researcher", ""],
      ["planner", "calc_researcher", ""], ["rag_researcher", "blackboard", "read/write"],
      ["analyst", "blackboard", "read/write"], ["supervisor", "analyst", "A2A"],
      ["supervisor", "writer", "A2A"], ["supervisor", "critic", "A2A"],
      ["rag_researcher", "rag_tool", "MCP"], ["web_researcher", "web_tool", "MCP"],
      ["calc_researcher", "calc_tool", "MCP"],
    ],
    hierarchical: [
      ["user", "supervisor", ""], ["supervisor", "planner", ""],
      ["supervisor", "rag_researcher", ""], ["supervisor", "analyst", "A2A"],
      ["supervisor", "writer", "A2A"], ["supervisor", "critic", "A2A"],
      ["writer", "critic", "revision"], ["analyst", "blackboard", "read/write"],
      ["rag_researcher", "blackboard", "read/write"],
      ["rag_researcher", "rag_tool", "MCP"],
    ],
    p2p: [
      ["user", "supervisor", ""], ["supervisor", "planner", ""],
      ["planner", "rag_researcher", ""], ["rag_researcher", "analyst", "A2A"],
      ["analyst", "writer", "A2A"], ["writer", "critic", "A2A"],
      ["rag_researcher", "rag_tool", "MCP"], ["analyst", "blackboard", "audit"],
    ],
  };

  function diagramFor(pattern) {
    const key = PATTERN_EDGES[pattern] ? pattern : "sequential";
    return {
      pattern: key,
      nodes: NODES.slice(),
      edges: PATTERN_EDGES[key].map(([from, to, label]) => ({ from, to, label })),
      blackboardAuditOnly: key === "p2p",
    };
  }

  function diagramState(events, pattern) {
    const nodes = {};
    NODES.forEach((id) => { nodes[id] = { state: "idle", label: "", bytes: "", calls: 0 }; });

    const toolOf = {
      rag_researcher: "rag_tool", web_researcher: "web_tool", calc_researcher: "calc_tool",
    };
    const edges = {};
    const spec = diagramFor(pattern);
    spec.edges.forEach((edge) => {
      edges[edge.from + "->" + edge.to] = { from: edge.from, to: edge.to,
        label: edge.label, bytes: "", active: false };
    });

    (events || []).forEach((event) => {
      const agent = event.agent || "";
      const node = nodes[agent];
      if (event.kind === "stage_start" && node) node.state = "running";
      if (event.kind === "stage_end" && node) {
        node.state = event.ok === false ? "failed" : "done";
        node.label = duration(event.duration_ms);
      }
      if (event.kind === "tool_call" && toolOf[agent]) {
        const tool = nodes[toolOf[agent]];
        tool.calls += 1;
        tool.state = "running";
      }
      if (event.kind === "a2a_call") {
        const extra = event.extra || {};
        const label = bytes(extra.request_bytes || 0) + " out / "
          + bytes(extra.response_bytes || 0) + " in";
        if (node) node.bytes = label;
        Object.keys(edges).forEach((key) => { edges[key].active = false; });
        const key = "supervisor->" + agent;
        if (edges[key]) {
          edges[key].bytes = label;
          edges[key].active = true;
        } else if (edges[agent + "->" + "writer"]) {
          edges[agent + "->writer"].bytes = label;
          edges[agent + "->writer"].active = true;
        }
      }
      if (event.kind === "blackboard_write") {
        nodes.blackboard.state = "running";
      }
    });

    return { nodes, edges, a2a: a2aBytes(events), pattern: spec.pattern };
  }

  /* "just now" / "5 m ago" / "2 h ago" / "yesterday" / "3 d ago". */
  function relativeTime(iso, now) {
    const stamp = Date.parse(iso || "");
    if (!stamp) return "";
    const current = now ? new Date(now).getTime() : Date.now();
    const seconds = Math.max(0, Math.round((current - stamp) / 1000));
    if (seconds < 60) return "just now";
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + " m ago";
    const hours = Math.round(minutes / 60);
    if (hours < 24) return hours + " h ago";
    const days = Math.round(hours / 24);
    return days === 1 ? "yesterday" : days + " d ago";
  }

  /* History rows: Today / Yesterday / Earlier, consecutive duplicates collapsed. */
  function groupHistory(runs, options) {
    const opts = options || {};
    const now = opts.now ? new Date(opts.now) : new Date();
    const limit = opts.limit || 8;
    const sorted = (runs || []).slice().sort((a, b) =>
      String(b.started_at || "").localeCompare(String(a.started_at || "")));

    const rows = [];
    sorted.forEach((run) => {
      const question = String(run.question || "").replace(/\s+/g, " ").trim();
      const last = rows[rows.length - 1];
      if (last && last.question === question) {
        last.count += 1;
        return;
      }
      rows.push({
        run_id: run.run_id, question, pattern: run.pattern || "?",
        approved: run.approved === undefined ? null : run.approved,
        severity: run.severity || null,
        started_at: run.started_at || "",
        when: relativeTime(run.started_at, now),
        count: 1,
      });
    });

    const groups = [];
    const dayOf = (iso) => {
      const stamp = Date.parse(iso);
      if (!stamp) return 2;
      const then = new Date(stamp);
      const days = Math.floor((now - then) / 86400000);
      return days <= 0 ? 0 : days === 1 ? 1 : 2;
    };
    const labels = ["Today", "Yesterday", "Earlier"];

    rows.forEach((row) => {
      const index = dayOf(row.started_at);
      let group = groups.find((item) => item.day === index);
      if (!group) {
        group = { day: index, label: labels[index], items: [] };
        groups.push(group);
      }
      group.items.push(row);
    });
    groups.sort((a, b) => a.day - b.day);

    let shown = 0;
    let hidden = 0;
    const limited = groups.map((group) => {
      const items = [];
      group.items.forEach((item) => {
        if (shown < limit) { items.push(item); shown += 1; } else { hidden += 1; }
      });
      return { ...group, items };
    }).filter((group) => group.items.length);
    return { groups: limited, hidden };
  }

  /* Run view tabs: start moves to activity, finish returns to answer. */
  function tabReducer(state, action) {
    const current = state || { tab: "answer", running: false };
    switch (action.type) {
      case "start":
        return { tab: "activity", running: true };
      case "finish":
        return { tab: "answer", running: false };
      case "select":
        return { tab: action.tab || "answer", running: current.running };
      default:
        return current;
    }
  }

  function progressStrip(events) {
    const rows = reduceTimeline(events);
    return rows.slice(0, 6).map((row) => row.agent + " " + row.status).join(" \u00b7 ");
  }

  /* ------------------------------------------------------------ activity trace */

  const AGENT_LABELS = {
    planner: "Planner",
    researcher: "Research worker",
    rag_researcher: "RAG researcher",
    web_researcher: "Web researcher",
    calc_researcher: "Calculation researcher",
    analyst: "Analyst",
    writer: "Writer",
    critic: "Critic",
    supervisor: "Supervisor",
  };

  function agentLabel(agent) {
    const name = String(agent || "").trim();
    if (!name) return "athena";
    return AGENT_LABELS[name] || name.replace(/[_-]+/g, " ");
  }

  function normaliseToolSelection(value, ids) {
    const all = Array.isArray(ids) ? ids.filter(Boolean) : [];
    if (value === null || value === undefined) return null;
    const selected = all.filter((id) => Array.isArray(value) && value.includes(id));
    if (all.length && selected.length === all.length) return null;
    return selected;
  }

  function toggleToolSelection(value, id, ids) {
    const all = (ids || []).filter(Boolean);
    if (id === "__all__") return null;
    if (!id || !all.includes(id)) return normaliseToolSelection(value, all);
    let selected = value === null ? [] : (Array.isArray(value) ? value.slice() : []);
    if (selected.includes(id)) selected = selected.filter((item) => item !== id);
    else selected.push(id);
    const next = all.filter((item) => selected.includes(item));
    return next.length && next.length === all.length ? null : next;
  }

  function toolSelectionLabel(value) {
    if (value === null || value === undefined) return "All tools";
    if (!value.length) return "No tools";
    return value.length + " tool" + (value.length === 1 ? "" : "s");
  }

  function toolNameFromSummary(summary) {
    const text = String(summary || "").trim();
    if (!text) return "";
    return text.split(":", 1)[0].trim();
  }

  function traceStatus(status) {
    if (status === "failed" || status === "error") return "failed";
    if (status === "skipped") return "skipped";
    if (status === "running" || status === "pending") return status;
    if (status === "stopped") return "stopped";
    return "done";
  }

  function traceElapsed(rows, fallback) {
    const stamps = (rows || [])
      .map((row) => Date.parse(row.ts || ""))
      .filter((value) => Number.isFinite(value));
    if (stamps.length > 1) return Math.max(...stamps) - Math.min(...stamps);
    const durations = (rows || [])
      .map((row) => Number(row.duration))
      .filter((value) => Number.isFinite(value) && value >= 0);
    if (durations.length) return Math.max(...durations);
    return Number(fallback) || 0;
  }

  function traceCurrent(rows) {
    const list = rows || [];
    for (let index = list.length - 1; index >= 0; index -= 1) {
      if (list[index].status === "running") return list[index];
    }
    for (let index = list.length - 1; index >= 0; index -= 1) {
      if (list[index].status === "failed") return list[index];
    }
    return list.length ? list[list.length - 1] : null;
  }

  function traceSummary(rows) {
    const list = rows || [];
    if (!list.length) return "No activity recorded";
    const failed = list.filter((row) => row.status === "failed").length;
    const skipped = list.filter((row) => row.status === "skipped").length;
    const parts = [list.length + " step" + (list.length === 1 ? "" : "s")];
    if (failed) parts.push(failed + " failed");
    if (skipped) parts.push(skipped + " skipped");
    return parts.join(" \u00b7 ");
  }

  function researchTrace(events, options) {
    const opts = options || {};
    const rows = [];
    const stageIndex = new Map();
    (events || []).forEach((event, index) => {
      if (!event || typeof event !== "object") return;
      const kind = event.kind;
      const agent = event.agent || "supervisor";
      const id = event.event_id || "ev-" + index;
      if (kind === "stage_start") {
        const framework = event.framework || frameworkOf(event);
        const remote = framework === "crewai";
        const a2aRole = ["analyst", "writer", "critic"].includes(agent);
        const row = {
          id: "stage-" + framework + "-" + agent + "-" + index,
          agent,
          label: agentLabel(agent)
            + (remote ? " (remote)" : (a2aRole ? " (handoff)" : "")),
          detail: remote ? "CrewAI stage started"
            : (a2aRole ? "A2A wrapper started" : "started"),
          status: "running",
          duration: null,
          framework,
          ts: event.ts || "",
          error: "",
        };
        rows.push(row);
        const key = framework + ":" + agent;
        stageIndex.set(key, (stageIndex.get(key) || []).concat(row));
      } else if (kind === "stage_end") {
        const framework = event.framework || frameworkOf(event);
        const stack = stageIndex.get(framework + ":" + agent) || [];
        const row = stack.pop();
        if (row) {
          row.status = event.ok === false ? "failed" : "done";
          row.duration = event.duration_ms == null ? row.duration : event.duration_ms;
          row.error = event.error_message || row.error;
          row.detail = row.status === "failed"
            ? (event.error_message || "failed")
            : (framework === "crewai" ? "CrewAI stage completed" : "completed");
        } else {
          const remote = framework === "crewai";
          const a2aRole = ["analyst", "writer", "critic"].includes(agent);
          rows.push({
            id: "stage-" + framework + "-" + agent + "-" + index,
            agent,
            label: agentLabel(agent)
              + (remote ? " (remote)" : (a2aRole ? " (handoff)" : "")),
            detail: event.payload_summary || (remote ? "CrewAI stage completed" : "completed"),
            status: event.ok === false ? "failed" : "done",
            duration: event.duration_ms == null ? null : event.duration_ms,
            framework,
            ts: event.ts || "",
            error: event.error_message || "",
          });
        }
      } else if (kind === "recovery") {
        const extra = event.extra || {};
        rows.push({
          id: "recovery-" + id,
          agent: "supervisor",
          label: extra.skipped ? "Recovery skipped" : "Research recovery",
          detail: event.payload_summary || (extra.reason || ""),
          status: extra.skipped ? "skipped" : "done",
          duration: null,
          framework: "adk",
          ts: event.ts || "",
          error: "",
        });
      } else if (kind === "tool_call") {
        const tool = toolNameFromSummary(event.payload_summary) || "tool";
        const sources = Array.isArray(event.extra && event.extra.sources)
          ? event.extra.sources : [];
        const sourceText = sources.map((source) => {
          if (source.title) return source.title;
          if (source.url) return source.url;
          return (source.document || "") + (source.page ? " p" + source.page : "");
        }).filter(Boolean).slice(0, 3).join(", ");
        rows.push({
          id: "tool-" + id,
          agent,
          label: tool,
          detail: [event.payload_summary || tool, sourceText].filter(Boolean).join(" \u00b7 "),
          status: event.ok === false ? "failed" : "done",
          duration: event.duration_ms == null ? null : event.duration_ms,
          framework: event.framework || "adk",
          ts: event.ts || "",
          error: event.error_message || "",
          tool,
        });
      } else if (kind === "a2a_call") {
        const extra = event.extra || {};
        rows.push({
          id: "a2a-" + id,
          agent,
          label: "A2A handoff",
          detail: bytes(extra.request_bytes || 0) + " out / " + bytes(extra.response_bytes || 0) + " in",
          status: event.ok === false ? "failed" : "done",
          duration: event.duration_ms == null ? null : event.duration_ms,
          framework: event.framework || "crewai",
          ts: event.ts || "",
          error: event.error_message || "",
        });
      } else if (kind === "error") {
        rows.push({
          id: "error-" + id,
          agent,
          label: event.error_type || "Error",
          detail: event.payload_summary || "",
          status: "failed",
          duration: event.duration_ms == null ? null : event.duration_ms,
          framework: event.framework || frameworkOf(event),
          ts: event.ts || "",
          error: event.error_message || "",
        });
      } else if (kind === "skip") {
        rows.push({
          id: "skip-" + id,
          agent,
          label: "Skipped",
          detail: event.payload_summary || "not required",
          status: "skipped",
          duration: null,
          framework: event.framework || frameworkOf(event),
          ts: event.ts || "",
          error: "",
        });
      }
    });

    if (opts.summary) {
      const summary = opts.summary;
      if (summary.critic_status) {
        const verdict = String(summary.critic_status);
        rows.push({
          id: "summary-critic",
          agent: "critic",
          label: "Verdict",
          detail: verdict + (summary.critique_reason ? " \u00b7 " + summary.critique_reason : ""),
          status: verdict === "approved" ? "done"
            : verdict === "rejected" || verdict === "failed" ? "failed" : "skipped",
          duration: null,
          framework: "crewai",
          ts: "",
          error: "",
        });
      }
      const revisions = Number(summary.revision_rounds || 0);
      if (revisions > 0) {
        rows.push({
          id: "summary-revisions",
          agent: "supervisor",
          label: "Revisions",
          detail: revisions + " revision round" + (revisions === 1 ? "" : "s"),
          status: "done",
          duration: null,
          framework: "adk",
          ts: "",
          error: "",
        });
      }
      (summary.unmet_requirements || []).forEach((issue, index) => {
        rows.push({
          id: "summary-gap-" + index,
          agent: "supervisor",
          label: "Evidence gap",
          detail: String(issue),
          status: summary.evidence_sufficient === false ? "failed" : "skipped",
          duration: null,
          framework: "adk",
          ts: "",
          error: "",
        });
      });
    }

    // A run that has terminated must not leave a row spinning forever.
    if (opts.finished) {
      rows.forEach((row) => {
        if (row.status === "running") {
          row.status = "done";
          row.detail = row.detail || "finished with the run";
        }
      });
    }
    const current = traceCurrent(rows);
    return {
      rows,
      current,
      running: rows.some((row) => row.status === "running"),
      failed: rows.some((row) => row.status === "failed"),
      elapsed_ms: traceElapsed(rows, opts.elapsed_ms),
      summary: traceSummary(rows),
    };
  }

  function chatTrace(events) {
    const rows = [];
    const indexes = new Map();
    (events || []).forEach((event, index) => {
      if (!event || typeof event !== "object") return;
      const id = event.id || "trace-" + index;
      const detail = [event.summary, event.error].filter(Boolean).join(" \u00b7 ");
      const row = {
        id,
        agent: "athena",
        label: event.label || event.stage || "Activity",
        detail,
        status: traceStatus(event.status),
        duration: event.elapsed_ms == null ? null : event.elapsed_ms,
        framework: "chat",
        ts: event.at || "",
        error: event.error || "",
        tool: event.tool || "",
      };
      if (indexes.has(id)) rows[indexes.get(id)] = row;
      else {
        indexes.set(id, rows.length);
        rows.push(row);
      }
    });
    const current = traceCurrent(rows);
    return {
      rows,
      current,
      running: rows.some((row) => row.status === "running"),
      failed: rows.some((row) => row.status === "failed"),
      elapsed_ms: traceElapsed(rows),
      summary: traceSummary(rows),
    };
  }

  /* ------------------------------------------------------- response context */

  function safeHttpUrl(value) {
    const raw = String(value || "").trim();
    if (!/^https?:\/\//i.test(raw)) return "";
    try {
      const parsed = new URL(raw);
      if (parsed.username || parsed.password) return "";
      return parsed.toString();
    } catch (_) {
      return "";
    }
  }

  function hostOf(value) {
    const url = safeHttpUrl(value);
    if (!url) return "";
    try { return new URL(url).hostname.replace(/^www\./, ""); } catch (_) { return ""; }
  }

  function filenameFromRef(ref) {
    const head = String(ref || "").split("#", 1)[0];
    return head.split(/[\\/]/).filter(Boolean).pop() || head || "document";
  }

  function pageFromRef(ref) {
    const match = /#p(\d+)/i.exec(String(ref || ""));
    return match ? Number(match[1]) : null;
  }

  function firstLine(value) {
    return String(value || "").split(/\r?\n/).map((line) => line.trim()).find(Boolean) || "";
  }

  function excerptOf(value, limit = 240) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > limit ? text.slice(0, limit).trimEnd() + "\u2026" : text;
  }

  function uniqueStrings(values) {
    return [...new Set((values || []).filter(Boolean).map((value) => String(value)))];
  }

  function passageCountFromText(value) {
    const match = /(\d+)\s+passage/i.exec(String(value || ""));
    return match ? Number(match[1]) : 0;
  }

  function stateTone(state) {
    if (state === "failed" || state === "error") return "error";
    if (state === "rejected" || state === "incomplete" || state === "stopped") return "warn";
    if (state === "running" || state === "pending" || state === "completing") return "live";
    if (state === "enabled_not_run" || state === "off" || state === "empty" || state === "recorded") return "muted";
    return "ok";
  }

  function chatExecutionStatus(message) {
    const status = String(message && message.status || "");
    if (status === "streaming") return { state: "running", label: "Running", tone: "live" };
    if (status === "stopped") return { state: "stopped", label: "Stopped", tone: "warn" };
    if (status === "error") return { state: "failed", label: "Failed", tone: "error" };
    if (status === "ok") return { state: "completed", label: "Completed", tone: "ok" };
    return { state: "recorded", label: "Recorded", tone: "muted" };
  }

  function researchExecutionStatus(summary, phase) {
    const current = String(phase || "");
    if (current === "starting") return { state: "queued", label: "Queued", tone: "live" };
    if (current === "running") return { state: "running", label: "Researching", tone: "live" };
    if (current === "completing") return { state: "completing", label: "Completing", tone: "live" };
    if (summary && summary.failed) return { state: "failed", label: "Failed", tone: "error" };
    if (summary && summary.outcome === "rejected") {
      return { state: "rejected", label: "Rejected", tone: "warn" };
    }
    if (summary && (summary.outcome === "incomplete" || summary.evidence_sufficient === false)) {
      return { state: "incomplete", label: "Incomplete", tone: "warn" };
    }
    if (summary && summary.critic_status === "rejected") {
      return { state: "rejected", label: "Rejected", tone: "warn" };
    }
    if (summary && summary.outcome === "approved") {
      return { state: "completed", label: "Completed", tone: "ok" };
    }
    if (current === "history" || current === "replay") {
      return { state: "recorded", label: "Recorded", tone: "muted" };
    }
    return { state: "recorded", label: "Recorded", tone: "muted" };
  }

  function researchRagState(input) {
    const toolReport = input.toolReport || {};
    const task = input.task || {};
    const evidence = input.evidence || [];
    const events = input.events || [];
    const active = Boolean(input.active);
    const hasTask = Boolean(input.task && Object.keys(input.task).length);
    const hasReport = Array.isArray(toolReport.enabled);
    if (!hasTask && !hasReport && active) {
      return {
        state: "pending",
        label: "RAG pending",
        count: 0,
        detail: "Waiting for the run's tool selection.",
      };
    }
    const enabled = hasReport
      ? toolReport.enabled.includes("rag_search")
      : (task.tools === null || task.tools === undefined || task.tools.includes("rag_search"));

    let calls = (toolReport.calls || []).filter((call) => call.tool === "rag_search");
    if (!calls.length) {
      calls = events
        .filter((event) => event.kind === "tool_call"
          && toolNameFromSummary(event.payload_summary) === "rag_search")
        .map((event) => ({ tool: "rag_search", ok: event.ok !== false, error: event.error_message || "" }));
    }
    const ragEvidence = evidence.filter((item) => item.source_type === "rag").length;
    const failed = calls.some((call) => !call.ok)
      || (toolReport.failed || []).includes("rag_search");

    if (!enabled) {
      return {
        state: "off",
        label: "RAG off",
        count: 0,
        detail: "Document retrieval was not enabled for this run.",
      };
    }
    if (failed) {
      const error = calls.find((call) => !call.ok);
      return {
        state: "failed",
        label: "RAG failed",
        count: 0,
        detail: (error && error.error) || "Document retrieval failed.",
      };
    }
    if (ragEvidence > 0) {
      return {
        state: "passages",
        label: "RAG " + ragEvidence + " passage" + (ragEvidence === 1 ? "" : "s"),
        count: ragEvidence,
        detail: ragEvidence + " document passage(s) were recorded.",
      };
    }
    if (calls.length) {
      return {
        state: "empty",
        label: "RAG 0 passages",
        count: 0,
        detail: "Document retrieval ran but returned no passages.",
      };
    }
    if (active) {
      return {
        state: "pending",
        label: "RAG pending",
        count: 0,
        detail: "Document retrieval has not completed yet.",
      };
    }
    return {
      state: "enabled_not_run",
      label: "RAG not run",
      count: 0,
      detail: "Documents were enabled, but no document retrieval was recorded.",
    };
  }

  function chatRagState(input) {
    const capabilities = input.capabilities || [];
    const sources = input.sources || [];
    const trace = input.trace || [];
    const status = String(input.status || "");
    const enabled = capabilities.includes("rag");
    const documents = sources.filter((source) => source.kind === "document").length;
    const retrieval = trace.find((event) => event.tool === "rag_search"
      || event.id === "retrieval-rag_search");

    if (retrieval) {
      if (retrieval.status === "running") {
        return { state: "running", label: "RAG running", count: documents, detail: "Document retrieval is in progress." };
      }
      if (retrieval.status === "error" || retrieval.status === "failed") {
        return {
          state: "failed",
          label: "RAG failed",
          count: documents,
          detail: retrieval.error || retrieval.detail || "Document retrieval failed.",
        };
      }
      const count = documents || passageCountFromText(retrieval.summary);
      if (count > 0) {
        return {
          state: "passages",
          label: "RAG " + count + " passage" + (count === 1 ? "" : "s"),
          count,
          detail: retrieval.summary || count + " document passage(s) were retrieved.",
        };
      }
      return { state: "empty", label: "RAG 0 passages", count: 0, detail: retrieval.summary || "No passages matched." };
    }
    if (!enabled) {
      if (documents > 0) {
        return { state: "passages", label: "RAG " + documents + " passage" + (documents === 1 ? "" : "s"), count: documents, detail: "Retrieved document context is attached." };
      }
      return { state: "off", label: "RAG off", count: 0, detail: "Document retrieval was disabled for this message." };
    }
    if (documents > 0) {
      return { state: "passages", label: "RAG " + documents + " passage" + (documents === 1 ? "" : "s"), count: documents, detail: "Retrieved document context is attached." };
    }
    if (status === "streaming") {
      return { state: "pending", label: "RAG pending", count: 0, detail: "Document retrieval has not reported yet." };
    }
    return { state: "enabled_not_run", label: "RAG not run", count: 0, detail: "Documents were enabled, but no retrieval event was recorded." };
  }

  function normaliseChatSources(sources) {
    return (sources || []).map((source, index) => {
      const document = source.kind === "document";
      const title = document
        ? (source.document || "Document")
        : (source.title || hostOf(source.url) || "Web source");
      const page = document ? (source.page == null ? null : source.page) : null;
      const position = source.index || index + 1;
      return {
        id: "chat-source-" + position,
        key: citationKey(position),
        index: position,
        citationIndex: position,
        title,
        type: document ? "Document" : "Web",
        location: document
          ? (page == null ? "Document passage" : "page " + page)
          : hostOf(source.url),
        url: document ? "" : safeHttpUrl(source.url),
        ref: source.chunk_id || source.document || source.url || "",
        page,
        score: source.score == null ? null : source.score,
        excerpt: excerptOf(source.snippet || ""),
        badges: ["Retrieved"],
      };
    });
  }

  function normaliseResearchSources(evidence, citations) {
    const citedRefs = new Set((citations || []).map((citation) => citation.source_ref).filter(Boolean));
    const citationByRef = new Map();
    (citations || []).forEach((citation, index) => {
      const ref = String(citation.source_ref || "");
      if (ref && !citationByRef.has(ref)) citationByRef.set(ref, index + 1);
    });
    const rows = [];
    const seen = new Set();
    (evidence || []).forEach((item, index) => {
      const sourceType = String(item.source_type || "other");
      const ref = String(item.source_ref || "");
      const id = String(item.id || "");
      const body = String(item.content || item.text || "");
      const fetched = sourceType === "web" && id.startsWith("fetch-");
      const failed = sourceType === "other" || /^fetch status:/i.test(body);
      const document = sourceType === "rag";
      const badges = [];
      if (failed) badges.push("Fetch failed");
      else if (fetched) badges.push("Page fetched");
      else if (sourceType === "web") badges.push("Search snippet");
      else if (document) badges.push("Document passage");
      else if (sourceType === "calculation") badges.push("Computed");
      else if (sourceType === "forecast") badges.push("Forecast");
      if (!failed) badges.push("Retrieved");
      if (citedRefs.has(ref)) badges.push("Cited in report");
      const citationIndex = citationByRef.get(ref) || null;
      rows.push({
        id: "research-source-" + (id || index),
        key: citationIndex ? String(citationIndex) : (ref ? "ref:" + ref : ""),
        citationIndex,
        title: document
          ? filenameFromRef(ref)
          : (firstLine(body) && !/^https?:\/\//i.test(firstLine(body))
            ? firstLine(body).slice(0, 100)
            : hostOf(ref) || "Web source"),
        type: failed ? "Provenance"
          : document ? "Document passage"
            : fetched ? "Fetched page"
              : sourceType === "web" ? "Search snippet"
                : sourceType === "calculation" ? "Calculation"
                  : sourceType === "forecast" ? "Forecast" : "Source",
        location: document ? filenameFromRef(ref) + (pageFromRef(ref) ? " \u00b7 page " + pageFromRef(ref) : "") : hostOf(ref) || ref,
        url: document ? "" : safeHttpUrl(ref),
        ref,
        page: pageFromRef(ref),
        score: item.score == null ? null : item.score,
        excerpt: excerptOf(body),
        badges,
        cited: citedRefs.has(ref),
        retrievedBy: item.retrieved_by || "",
      });
      seen.add(ref);
    });
    (citations || []).forEach((citation, index) => {
      const ref = String(citation.source_ref || "");
      if (!ref || seen.has(ref)) return;
      rows.push({
        id: "citation-" + index,
        key: String(index + 1),
        citationIndex: index + 1,
        title: hostOf(ref) || filenameFromRef(ref) || "Cited source",
        type: "Citation",
        location: ref,
        url: safeHttpUrl(ref),
        ref,
        page: pageFromRef(ref),
        score: null,
        excerpt: excerptOf(citation.quote_or_summary || citation.quote || ""),
        badges: ["Cited in report"],
        cited: true,
        retrievedBy: "",
      });
    });
    return rows;
  }

  function contextItems(items) {
    return (items || [])
      .filter((item) => item && item[0] && item[1] !== undefined && item[1] !== null && String(item[1]).trim() !== "")
      .map((item) => ({ label: String(item[0]), value: String(item[1]) }));
  }

  function chatContextModel(input) {
    const message = input.message || {};
    const userMessage = input.userMessage || null;
    const trace = Array.isArray(message.meta && message.meta.trace) ? message.meta.trace : [];
    const view = input.traceView || chatTrace(trace);
    const sources = normaliseChatSources(message.sources || []);
    const capabilities = Array.isArray(message.capabilities) ? message.capabilities : [];
    const rag = chatRagState({
      capabilities,
      sources: message.sources || [],
      trace,
      status: message.status,
    });
    const toolEvents = Array.isArray(message.tool_events) ? message.tool_events : [];
    const invoked = uniqueStrings(toolEvents.map((event) => event.name).filter(Boolean));
    const failedTools = uniqueStrings(toolEvents.filter((event) => event.status === "error").map((event) => event.name));
    const status = chatExecutionStatus(message);
    const meta = message.meta || {};
    const warnings = uniqueStrings([
      message.error,
      meta.tool_note,
      ...(Array.isArray(meta.grounding_notes) ? meta.grounding_notes : []),
    ]);
    const selectedTools = Array.isArray(message.tools) ? message.tools : [];
    const webEnabled = capabilities.includes("web_search");
    const webEvent = trace.find((event) => event.tool === "web_search" || event.id === "retrieval-web_search");
    const webSources = sources.filter((source) => source.type === "Web").length;
    const webDetail = webEvent && (webEvent.error || webEvent.summary)
      ? (webEvent.error || webEvent.summary)
      : (webSources ? webSources + " web source(s) retrieved." : "");
    return {
      kind: "chat",
      id: message.id || "",
      title: "Chat response",
      subtitle: [message.provider, message.model].filter(Boolean).join(" \u00b7 "),
      status,
      rag,
      sources,
      citations: [],
      citationCount: 0,
      run: {
        status,
        rows: view.rows,
        durationMs: view.elapsed_ms,
        tools: { selected: selectedTools, invoked, failed: failedTools, planned: [], skipped: [] },
        warnings,
        provider: message.provider || "",
        model: message.model || "",
        usage: message.usage || null,
        kind: "chat",
      },
      context: {
        prompt: userMessage ? String(userMessage.content || "") : "",
        items: contextItems([
          ["Provider", message.provider || ""],
          ["Model", message.model || ""],
          ["Execution", status.label],
          ["Documents / RAG", rag.label],
          ["Web search", !webEnabled ? "Off" : (webEvent ? (webEvent.status === "running" ? "Running" : webDetail || "Used") : "Enabled \u00b7 not run")],
          ["Selected tools", selectedTools.length ? selectedTools.join(", ") : "None"],
          ["Tool calls", toolEvents.length ? String(toolEvents.length) : ""],
          ["Retrieved sources", sources.length ? String(sources.length) : "0"],
          ["Usage", usageLabel(message.usage) || ""],
        ]),
        notes: uniqueStrings([
          ...warnings,
          sources.length ? "Chat sources are retrieved context; citation usage is not tracked." : "",
          "Document upload and attachment management are not implemented in this build.",
        ]),
      },
    };
  }

  function researchContextModel(input) {
    const snapshot = input.snapshot || {};
    const task = snapshot.task && snapshot.task.value ? snapshot.task.value : {};
    const report = snapshot.final_report && snapshot.final_report.value ? snapshot.final_report.value : null;
    const toolReport = snapshot.tool_report || {};
    const evidence = snapshot.evidence || [];
    const citations = report && Array.isArray(report.citations) ? report.citations : [];
    const events = input.events || [];
    const summary = input.summary || {};
    const phase = String(input.phase || "");
    const active = ["starting", "running", "completing"].includes(phase);
    const finished = ["completed", "completed_degraded", "completed_no_report", "failed", "ended_unconfirmed", "history", "replay"].includes(phase);
    const trace = researchTrace(events, { finished, summary });
    const status = researchExecutionStatus(summary, phase);
    const sources = normaliseResearchSources(evidence, citations);
    const rag = researchRagState({
      toolReport,
      task,
      evidence,
      events,
      active,
    });
    const selectedTools = task.tools === null || task.tools === undefined
      ? "All tools"
      : (task.tools.length ? task.tools.join(", ") : "No tools");
    const calls = Array.isArray(toolReport.calls) ? toolReport.calls : [];
    const invoked = Array.isArray(toolReport.invoked) ? toolReport.invoked : [];
    const failed = Array.isArray(toolReport.failed) ? toolReport.failed : [];
    const skipped = Array.isArray(toolReport.skipped) ? toolReport.skipped : [];
    const warnings = uniqueStrings([
      ...(Array.isArray(summary.unmet_requirements) ? summary.unmet_requirements : []),
      summary.critic_status === "rejected" ? summary.critique_reason : "",
    ]);
    const failures = uniqueStrings(failed.map((tool) => tool + " invocation failed"));
    const limitations = Array.isArray(summary.evidence_limitations)
      ? summary.evidence_limitations
      : [];
    const citationsCount = citations.length;
    const fetched = sources.filter((source) => source.badges.includes("Page fetched")).length;
    const distinct = new Set(sources.map((source) => source.ref).filter(Boolean)).size;
    const pattern = summary.pattern || task.pattern || input.pattern || "";
    const question = task.question || input.question || "";
    const durationMs = summary.wall_s ? Number(summary.wall_s) * 1000 : trace.elapsed_ms;
    return {
      kind: "research",
      id: input.runId || summary.run_id || "",
      title: "Research run",
      subtitle: [input.runId ? "run " + String(input.runId).slice(0, 8) : "", pattern].filter(Boolean).join(" \u00b7 "),
      status,
      rag,
      sources,
      citations,
      citationCount: citationsCount,
      run: {
        status,
        rows: trace.rows,
        durationMs,
        tools: {
          enabled: Array.isArray(toolReport.enabled) ? toolReport.enabled : [],
          planned: Array.isArray(toolReport.planned) ? toolReport.planned : [],
          plannedBlocked: Array.isArray(toolReport.planned_blocked) ? toolReport.planned_blocked : [],
          invoked,
          failed,
          skipped,
        },
        warnings,
        failures,
        pattern,
        criticStatus: summary.critic_status || "",
        criticReason: summary.critique_reason || "",
        outcome: summary.outcome || "",
        confidence: report && report.confidence != null ? report.confidence : null,
        canOpenDetails: true,
        kind: "research",
      },
      context: {
        prompt: question,
        items: contextItems([
          ["Pattern", pattern],
          ["Execution", status.label],
          ["Selected tools", selectedTools],
          ["Documents / RAG", rag.label],
          ["Evidence", evidence.length + " item" + (evidence.length === 1 ? "" : "s")],
          ["Fetched pages", String(fetched)],
          ["Distinct sources", String(distinct)],
          ["Citations", String(citationsCount)],
          ["Evidence limitations", String(limitations.length)],
          ["Confidence", report && report.confidence != null ? String(report.confidence) : ""],
          ["Critic", summary.critic_status || ""],
          ["Duration", durationMs ? duration(durationMs) : ""],
        ]),
        notes: uniqueStrings([
          ...warnings,
          ...failures,
          ...limitations.map((item) => "Limitation: " + item),
          "Document upload and attachment management are not implemented in this build.",
        ]),
      },
    };
  }

  function responseMetaFromModel(model) {
    if (!model) return [];
    const items = [];
    const status = model.status || { state: "recorded", label: "Recorded", tone: "muted" };
    items.push({
      id: "status",
      label: status.label,
      tone: status.tone || stateTone(status.state),
      action: "run",
      title: "Execution status",
    });
    const rag = model.rag || { state: "off", label: "RAG off" };
    items.push({
      id: "rag",
      label: rag.label,
      tone: stateTone(rag.state),
      action: "context",
      title: rag.detail || "Document retrieval status",
    });
    const sourceCount = (model.sources || []).length;
    items.push({
      id: "sources",
      label: "Sources " + sourceCount,
      tone: sourceCount ? "ok" : "muted",
      action: "sources",
      title: model.citationCount
        ? sourceCount + " retrieved source(s), " + model.citationCount + " cited in the final report"
        : sourceCount + " retrieved source(s)",
    });
    const tools = model.run && model.run.tools ? model.run.tools : {};
    const invokedCount = (tools.invoked || []).length;
    const selectedCount = (tools.selected || []).length;
    if (invokedCount || selectedCount || (tools.enabled || []).length) {
      items.push({
        id: "tools",
        label: invokedCount
          ? invokedCount + " tool" + (invokedCount === 1 ? "" : "s")
          : (selectedCount ? selectedCount + " selected" : (tools.enabled || []).length + " enabled"),
        tone: (tools.failed || []).length ? "warn" : "muted",
        action: "run",
        title: "Tool usage",
      });
    }
    const warnings = (model.run && model.run.warnings) || [];
    if (warnings.length) {
      items.push({
        id: "warnings",
        label: warnings.length + (model.kind === "research" ? " gap" : " warning") + (warnings.length === 1 ? "" : "s"),
        tone: "warn",
        action: "context",
        title: warnings[0],
      });
    }
    items.push({
      id: "context",
      label: "Context",
      tone: "muted",
      action: "context",
      title: "Prompt, tools, retrieval and associated context",
    });
    return items;
  }

  function chatResponseMeta(input) {
    return responseMetaFromModel(chatContextModel(input));
  }

  function researchResponseMeta(input) {
    return responseMetaFromModel(researchContextModel(input));
  }

  /* ------------------------------------------------------------ chat helpers */

  function sseParser() {
    let buffer = "";
    function parseBlock(block) {
      const lines = block.split(/\r?\n/);
      let event = "message";
      let id = "";
      const data = [];
      lines.forEach((line) => {
        if (!line || line.startsWith(":")) return;
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
        else if (line.startsWith("id:")) id = line.slice(3).trim();
      });
      if (!data.length) return null;
      return { event, id, data: data.join("\n") };
    }
    function drain(final) {
      const out = [];
      let match = /\r?\n\r?\n/.exec(buffer);
      while (match) {
        const block = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        const parsed = parseBlock(block);
        if (parsed) out.push(parsed);
        match = /\r?\n\r?\n/.exec(buffer);
      }
      if (final && buffer.trim()) {
        const parsed = parseBlock(buffer);
        if (parsed) out.push(parsed);
        buffer = "";
      }
      return out;
    }
    return {
      push(chunk) { buffer += String(chunk == null ? "" : chunk); return drain(false); },
      flush() { return drain(true); },
    };
  }

  function usageLabel(usage) {
    if (!usage) return "";
    const input = Number(usage.tokens_in || 0);
    const output = Number(usage.tokens_out || 0);
    if (!input && !output) return "";
    const reasoning = Number(usage.reasoning_tokens || 0);
    return input.toLocaleString() + " in \u00b7 " + output.toLocaleString() + " out"
      + (reasoning ? " \u00b7 " + reasoning.toLocaleString() + " reasoning" : "");
  }

  function toolSummary(events) {
    const rows = Array.isArray(events) ? events : [];
    if (!rows.length) return "";
    const failed = rows.filter((row) => row.status === "error" || row.status === "failed").length;
    const label = rows.length + " tool" + (rows.length === 1 ? "" : "s");
    return failed ? label + " \u00b7 " + failed + " failed" : label + " \u00b7 " + rows.length + " ok";
  }

  function sourceLabel(source) {
    if (!source) return "";
    if (source.kind === "web") {
      try { return new URL(source.url).hostname.replace(/^www\./, ""); } catch (_) {
        return source.title || source.url || "web";
      }
    }
    if (source.kind === "document") {
      return String(source.document || "document") + (source.page ? " \u00b7 p" + source.page : "");
    }
    return String(source.title || source.document || source.url || "source");
  }

  function filterSettingsSections(sections, query) {
    const rows = Array.isArray(sections) ? sections : [];
    const needle = String(query == null ? "" : query).trim().toLowerCase();
    if (!needle) return rows.map((section) => section.id);
    return rows.filter((section) => {
      const hay = [section.label, section.id].concat(section.keywords || []).join(" ").toLowerCase();
      return hay.includes(needle);
    }).map((section) => section.id);
  }

  function nextFocusIndex(count, current, shift) {
    const total = Math.max(0, Number(count) || 0);
    if (!total) return 0;
    const step = shift ? -1 : 1;
    return (Number(current || 0) + step + total) % total;
  }

  /* ------------------------------------------------------- Capillary options */

  // The exact requested background configuration, kept in one place. The GPU
  // preset has no public field for `bayer4` (that belonged to the retired canvas
  // templates), and Capillary's simulation owns the scene/text/dither, so those
  // requested values are documented here but not passed as fake API options.
  const CAPILLARY_REQUESTED = Object.freeze({
    renderStyle: "original", look: "", algorithm: "bayer4", dither: 0,
    scheme: "dark", ink: "#f2f0ea", speed: 1, size: 10, opacity: 1,
    characters: "detailed", word: "ASCIIFY", scene: "flow", hover: "lantern", mark: "glyph",
  });

  function capillaryOptions(clear) {
    return {
      palette: "var(--capillary-ink, #f2f0ea)",
      speed: CAPILLARY_REQUESTED.speed,
      cellSize: CAPILLARY_REQUESTED.size,
      opacity: CAPILLARY_REQUESTED.opacity,
      charset: CAPILLARY_REQUESTED.characters,
      cursor: CAPILLARY_REQUESTED.hover,
      mark: CAPILLARY_REQUESTED.mark,
      dither: CAPILLARY_REQUESTED.dither,
      text: CAPILLARY_REQUESTED.word,
      look: { scene: CAPILLARY_REQUESTED.scene },
      clear: clear || null,
      globalLook: false,
      lazy: true,
    };
  }

  /* -------------------------------------------------------- tutorial helpers */

  function tutorialPlacement(target, card, viewport, preferred) {
    const pad = 12;
    const gap = 12;
    const vw = Math.max(1, Number(viewport && viewport.width) || 1024);
    const vh = Math.max(1, Number(viewport && viewport.height) || 768);
    const tw = Math.max(1, Number(target && target.width) || 0);
    const th = Math.max(1, Number(target && target.height) || 0);
    const cw = Math.min(Number(card && card.width) || 340, vw - pad * 2);
    const ch = Math.min(Number(card && card.height) || 220, vh - pad * 2);
    const left = Number(target && target.left) || 0;
    const top = Number(target && target.top) || 0;
    if (vw <= 640) {
      return { placement: "bottom", mobile: true, left: pad, top: Math.max(pad, vh - ch - pad), width: vw - pad * 2 };
    }
    const space = {
      bottom: vh - (top + th) - pad,
      top: top - pad,
      right: vw - (left + tw) - pad,
      left: left - pad,
    };
    const fits = (place) => (place === "top" || place === "bottom"
      ? space[place] >= ch + gap
      : space[place] >= cw + gap);
    let placement = ["top", "bottom", "left", "right"].includes(preferred) ? preferred : "bottom";
    if (!fits(placement)) {
      placement = ["bottom", "top", "right", "left"].find(fits) || "bottom";
    }
    let x = left + tw / 2 - cw / 2;
    let y = top + th + gap;
    if (placement === "top") y = top - ch - gap;
    if (placement === "right") { x = left + tw + gap; y = top + th / 2 - ch / 2; }
    if (placement === "left") { x = left - cw - gap; y = top + th / 2 - ch / 2; }
    x = Math.max(pad, Math.min(vw - cw - pad, x));
    y = Math.max(pad, Math.min(vh - ch - pad, y));
    return { placement, mobile: false, left: x, top: y, width: cw };
  }

  function tutorialProgress(index, total) {
    return "Step " + (Number(index) + 1) + " of " + total;
  }

  function parseTutorialState(raw) {
    if (!raw) return null;
    try {
      const value = typeof raw === "string" ? JSON.parse(raw) : raw;
      if (value && (value.status === "done" || value.status === "skipped")) {
        return { status: value.status, step: Math.max(0, Number(value.step) || 0) };
      }
    } catch (_) { /* unreadable storage is the same as no state */ }
    return null;
  }

  function shouldAutoStartTutorial(boot, state, busy) {
    if (state) return false;
    if (busy) return false;
    const view = String(boot && boot.view || "run");
    if (view.startsWith("replay=")) return false;
    if (["settings", "details", "how"].includes(view)) return false;
    if (boot && boot.params && boot.params.at !== undefined) return false;
    return true;
  }

  /* -------------------------------------------------------- event log rows */

  /* Every kind athena records today (`athena/observability/events.py`) plus the
     two the API writes itself. The log renders all of them; an unknown kind is
     still shown by name rather than dropped. */
  const EVENT_LOG_KINDS = Object.freeze([
    "stage_start", "stage_end", "model_call", "tool_call", "a2a_call",
    "recovery", "blackboard_write", "limiter_wait", "skip", "error",
    "retry", "guardrail", "handoff", "done",
  ]);

  function eventSummary(event) {
    const extra = event.extra || {};
    return String(event.payload_summary || event.summary || extra.reason || "");
  }

  function eventLogEntry(event) {
    if (!event || typeof event !== "object") return null;
    const kind = String(event.kind || "event");
    const extra = event.extra || {};
    const agent = String(event.agent || "");
    const summary = eventSummary(event);
    const row = { kind, label: kind, text: "", tone: "" };
    if (kind === "a2a_call") {
      row.label = "a2a";
      row.text = agent + " " + bytes(extra.request_bytes) + " out / "
        + bytes(extra.response_bytes) + " in";
    } else if (kind === "tool_call") {
      row.label = "tool";
      row.tone = event.ok === false ? "error" : "tool";
      row.text = summary || agent;
    } else if (kind === "blackboard_write") {
      row.label = "board";
      row.text = summary || agent;
    } else if (kind === "error") {
      row.label = "error";
      row.tone = "error";
      row.text = (event.error_type || "error") + ": " + (event.error_message || summary);
    } else if (kind === "stage_end") {
      row.label = "stage";
      row.text = [
        agent,
        duration(event.duration_ms),
        tokens(event.tokens_in, event.tokens_out,
          event.tokens_in != null || event.tokens_out != null),
      ].filter(Boolean).join(" \u00b7 ");
    } else if (kind === "stage_start") {
      row.label = "stage";
      row.text = [agent + " started", extra.stage_scope].filter(Boolean).join(" \u00b7 ");
    } else if (kind === "model_call") {
      row.label = "model";
      row.text = [
        agent,
        extra.model,
        duration(event.duration_ms),
        tokens(event.tokens_in, event.tokens_out,
          event.tokens_in != null || event.tokens_out != null),
      ].filter(Boolean).join(" \u00b7 ");
    } else if (kind === "retry") {
      row.label = "retry";
      row.tone = "warn";
      const attempt = extra.attempt == null
        ? ""
        : "attempt " + extra.attempt + (extra.of == null ? "" : "/" + extra.of);
      row.text = [agent, attempt, summary.replace(/^retry:\s*/, "")].filter(Boolean).join(" \u00b7 ");
    } else if (kind === "guardrail") {
      row.label = "guardrail";
      row.tone = event.ok === false ? "error" : "warn";
      const rule = [extra.rule, extra.action].filter(Boolean).join(": ");
      row.text = [agent, rule || summary].filter(Boolean).join(" \u00b7 ");
    } else if (kind === "handoff") {
      row.label = "handoff";
      row.text = [agent, summary || (extra.to ? "to " + extra.to : "")].filter(Boolean).join(" \u00b7 ");
    } else if (kind === "recovery") {
      row.label = "recovery";
      row.tone = extra.skipped ? "muted" : "";
      row.text = summary || [agent, extra.role, extra.reason].filter(Boolean).join(" \u00b7 ");
    } else if (kind === "skip") {
      row.label = "skip";
      row.tone = "muted";
      row.text = summary || agent;
    } else if (kind === "limiter_wait") {
      row.label = "limit";
      row.tone = "warn";
      row.text = [agent, "waited " + duration(event.duration_ms),
        extra.tokens_requested == null ? "" : extra.tokens_requested + " tokens requested",
      ].filter(Boolean).join(" \u00b7 ");
    } else {
      row.text = [agent, summary].filter(Boolean).join(" \u00b7 ");
    }
    row.text = String(row.text || "").trim();
    return row;
  }

  function eventLogText(event) {
    const row = eventLogEntry(event);
    if (!row) return "";
    return row.label + " \u00b7 " + (row.text || row.kind);
  }

  /* --------------------------------------------------- citation markers */

  const CITATION_MARKER = /\[(\d{1,3})\]/g;

  /* Split an answer into text runs and ``[n]`` citation markers so the surface
     can render each marker as a real button. ``maxIndex`` (the source count)
     bounds what counts as a citation: an out-of-range ``[7]`` stays plain text
     instead of becoming a dead chip. */
  function answerSegments(value, maxIndex) {
    const body = String(value == null ? "" : value);
    const limit = Number(maxIndex);
    const bounded = Number.isFinite(limit) && limit > 0;
    const segments = [];
    let last = 0;
    CITATION_MARKER.lastIndex = 0;
    let match = CITATION_MARKER.exec(body);
    while (match) {
      const index = Number(match[1]);
      const usable = !bounded || (index >= 1 && index <= limit);
      if (usable) {
        if (match.index > last) {
          segments.push({ type: "text", text: body.slice(last, match.index) });
        }
        segments.push({ type: "citation", index, text: match[0] });
        last = match.index + match[0].length;
      }
      match = CITATION_MARKER.exec(body);
    }
    if (last < body.length) segments.push({ type: "text", text: body.slice(last) });
    if (!segments.length) segments.push({ type: "text", text: body });
    return segments;
  }

  /* The key a rendered citation marker uses to ask the context panel for one
     source: research citations are 1-based report citations, chat sources are
     their own index. */
  function citationKey(index) {
    const value = Number(index);
    return Number.isFinite(value) && value > 0 ? String(value) : "";
  }

  /* --- evaluation: operational vs research quality ------------------------
     These three are pure so the dashboard's measurement rules are testable.
     The rule they encode: an absent denominator or an absent sample is
     "not measurable", never 0%. */
  function evalPercent(metric) {
    if (!metric || typeof metric !== "object") return "\u2014";
    if (metric.rate == null) return Number(metric.n) ? "\u2014" : "not measurable";
    const pct = Math.round(Number(metric.rate) * 1000) / 10;
    return pct + "% (" + metric.hits + "/" + metric.n + ")";
  }

  function evalQualityLabel(operational) {
    const quality = (operational && operational.research_quality) || {};
    if (!quality.measurable) {
      return quality.display || "N/A - no valid completed research runs";
    }
    return Number(quality.mean).toFixed(3) + " (n=" + quality.n + ")";
  }

  function evalOperationalRows(operational) {
    if (!operational || !operational.attempts) return [];
    const terminal = operational.terminal_states || {};
    const causes = operational.failure_codes || {};
    const joiner = (object) => {
      const keys = Object.keys(object);
      return keys.length ? keys.map((key) => key + "=" + object[key]).join(", ") : "\u2014";
    };
    return [
      ["Attempts", String(operational.attempts)],
      ["Terminal states", joiner(terminal)],
      ["Failure causes", joiner(causes)],
      ["Model-start rate", evalPercent(operational.model_start)],
      ["Research completion", evalPercent(operational.research_completion)],
      ["Critic approval", evalPercent(operational.critic_approval)
        + " \u00b7 " + (operational.critic_evaluable_runs || 0) + " critic-evaluable"],
      ["Scoring success", evalPercent(operational.scoring_success)],
      ["Infrastructure failures", evalPercent(operational.infrastructure_failure)],
      ["  Blocked before any model", evalPercent(operational.infrastructure_blocked)],
      ["  Interrupted after a model", evalPercent(operational.interrupted_after_model)],
      ["Research quality", evalQualityLabel(operational)
        + " \u00b7 " + (operational.valid_completed_runs || 0) + " valid completed"],
    ];
  }

  const api = {
    frameworkOf, reduceTimeline, a2aBytes, duration, bytes, tokens,
    relativeTime, groupHistory, tabReducer, progressStrip,
    verdictLabel, verdictFromCritique, critiqueDiagnostic, citationRows, statusPill, blackboardCards,
    validateProfile, presetFill, requestState, isBusy,
    PATTERNS, DEFAULT_PATTERN, patternValue, validRunId,
    metricRows, metricValue,
    diagramFor, diagramState, NODES, PATTERN_EDGES,
    agentLabel, researchTrace, chatTrace, traceSummary,
    normaliseToolSelection, toggleToolSelection, toolSelectionLabel,
    safeHttpUrl, normaliseChatSources, normaliseResearchSources,
    chatContextModel, researchContextModel, responseMetaFromModel,
    chatResponseMeta, researchResponseMeta,
    chatExecutionStatus, researchExecutionStatus, chatRagState, researchRagState,
    sseParser, usageLabel, toolSummary, sourceLabel, filterSettingsSections, nextFocusIndex,
    CAPILLARY_REQUESTED, capillaryOptions,
    tutorialPlacement, tutorialProgress, parseTutorialState, shouldAutoStartTutorial,
    EVENT_LOG_KINDS, eventLogEntry, eventLogText,
    answerSegments, citationKey,
    evalPercent, evalQualityLabel, evalOperationalRows,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.AthenaLogic = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
