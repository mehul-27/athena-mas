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
        });
      }
      return rows.get(agent);
    };
    (events || []).forEach((event) => {
      const row = ensure(event.agent || "?");
      row.framework = frameworkOf(event);
      if (event.kind === "stage_end") {
        row.status = event.ok === false ? "failed" : "done";
        row.ms = event.duration_ms == null ? null : Math.round(event.duration_ms);
        if (event.tokens_in || event.tokens_out) {
          row.stage_in = event.tokens_in || 0;
          row.stage_out = event.tokens_out || 0;
        }
        if (event.extra && event.extra.stop_reason) row.stop_reason = event.extra.stop_reason;
      } else if (event.kind === "model_call") {
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

  function tokens(inCount, outCount) {
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
    if (plan) cards.push({ name: "plan", version: version("plan"),
      lines: (plan.steps || []).map((step, i) =>
        (i + 1) + ". " + (step.description || "") + " -> " + (step.assigned_to || "?")) });

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

  const api = {
    frameworkOf, reduceTimeline, a2aBytes, duration, bytes, tokens,
    relativeTime, groupHistory, tabReducer, progressStrip,
    verdictLabel, verdictFromCritique, critiqueDiagnostic, citationRows, statusPill, blackboardCards,
    validateProfile, presetFill, requestState, isBusy,
    PATTERNS, DEFAULT_PATTERN, patternValue, validRunId,
    metricRows, metricValue,
    diagramFor, diagramState, NODES, PATTERN_EDGES,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.AthenaLogic = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
