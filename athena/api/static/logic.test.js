/* Node tests for static/logic.js. Run: node athena/api/static/logic.test.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const logic = require("./logic.js");

const events = [
  { kind: "stage_start", agent: "planner", framework: "adk" },
  { kind: "model_call", agent: "planner", framework: "adk", tokens_in: 10, tokens_out: 2 },
  { kind: "stage_end", agent: "planner", framework: "adk", duration_ms: 1234.6, tokens_in: 10, tokens_out: 2 },
  { kind: "a2a_call", agent: "writer", framework: "adk", extra: { request_bytes: 156, response_bytes: 90 } },
  { kind: "stage_end", agent: "writer", framework: "crewai", duration_ms: 500, tokens_in: 300, tokens_out: 40, ok: true },
  { kind: "stage_end", agent: "critic", framework: "crewai", duration_ms: 20, ok: false },
  { kind: "tool_call", agent: "rag_researcher", summary: "rag_search" },
  { kind: "blackboard_write", agent: "analyst", summary: "write analysis v1" },
];

test("timeline derives done rows with duration and tokens", () => {
  const by = Object.fromEntries(logic.reduceTimeline(events).map((row) => [row.agent, row]));
  assert.equal(by.planner.status, "done");
  assert.equal(by.planner.ms, 1235);
  assert.equal(by.planner.tokens_in, 10);
  assert.equal(by.writer.framework, "crewai");
  assert.equal(by.critic.status, "failed");
  assert.equal(logic.reduceTimeline(events).some((row) => row.status === "running"), false);
});

test("a2a bytes add up", () => {
  assert.deepEqual(logic.a2aBytes(events), { out: 156, in: 90 });
});

test("formatting helpers", () => {
  assert.equal(logic.duration(950), "950 ms");
  assert.equal(logic.duration(1500), "1.5 s");
  assert.equal(logic.duration(null), "");
  assert.equal(logic.bytes(156), "156 B");
  assert.equal(logic.bytes(2048), "2.0 kB");
  assert.equal(logic.tokens(3, 4), "3 in / 4 out");
  assert.equal(logic.tokens(0, 0, false), "tokens unavailable");
});

test("verdict label never renders an element", () => {
  assert.equal(logic.verdictLabel(true, "low"), "critic approved (low)");
  assert.equal(logic.verdictLabel(false, "high"), "critic rejected (high)");
  assert.equal(logic.verdictLabel(null, null), "critic unknown");
});

test("citation rows always carry a quote", () => {
  const rows = logic.citationRows({ citations: [
    { source_ref: "nasa.pdf#p1", quote: "systems engineering is..." },
    { source_ref: "doc#p2" },
  ] });
  assert.equal(rows[1].quote, "(quote not recorded)");
});

test("status pill says replay when no provider", () => {
  const strip = logic.statusPill({ crew_server: "down", provider_configured: false }, false);
  assert.equal(strip.mode, "replay");
  assert.equal(strip.model, "n/a (replay)");

  const live = logic.statusPill({ crew_server: "up", provider_configured: true,
    model: "openai/x", api_host: "gateway.local" }, false);
  assert.equal(live.mode, "live");
  assert.equal(live.host, "gateway.local");
});

test("the active profile fills the status pill", () => {
  const strip = logic.statusPill({ crew_server: "up", provider_configured: true,
    active_profile: { model: "openai/fake", api_host: "gateway.example" } }, false);
  assert.equal(strip.model, "openai/fake");
  assert.equal(strip.host, "gateway.example");
});

test("blackboard cards render readable views", () => {
  const cards = logic.blackboardCards({
    task: { value: { question: "What is SE?", pattern: "sequential" }, version: 1 },
    plan: { value: { steps: [{ description: "search", assigned_to: "rag_researcher" }] }, version: 1 },
    evidence: [{ retrieved_by: "rag_researcher", source_ref: "nasa.pdf#p1", score: 0.9, text: "x".repeat(400) }],
    critique: { value: { approved: false, severity: "high", issues: ["no citation"] }, version: 2 },
  });
  const byName = Object.fromEntries(cards.map((card) => [card.name, card]));
  assert.deepEqual(byName.task.lines, ["What is SE?", "pattern: sequential"]);
  assert.equal(byName.evidence.rows[0][3].length, 200);
  assert.equal(byName.critique.verdict, "critic rejected (high)");
});

/* --- settings ------------------------------------------------------------ */

test("profile validation catches bad URLs and a missing key", () => {
  assert.equal(logic.validateProfile({ name: "a", model: "m", api_base: "https://x.example/v1" }).ok, true);
  assert.equal(logic.validateProfile({ name: "", model: "m" }).errors.name, "name is required");
  assert.equal(logic.validateProfile({ name: "a", model: "" }).errors.model, "model string is required");
  assert.equal(logic.validateProfile({ name: "a", model: "m", api_base: "http://x.example" }).errors.api_base,
    "http is only allowed for localhost");
  assert.equal(logic.validateProfile({ name: "a", model: "m", api_base: "http://localhost:8080/v1" }).ok, true);
  assert.equal(logic.validateProfile({ name: "a", model: "m", api_base: "https://u:p@x.example" }).errors.api_base,
    "the URL must not contain credentials");
  assert.equal(logic.validateProfile({ name: "a", model: "m", search_provider: "searxng" }).errors.search_endpoint,
    "SearXNG needs an endpoint");
  assert.equal(logic.validateProfile({ name: "a", model: "m", needs_key: true }).errors.llm_key,
    "an API key is required for a new provider");
});

test("a preset fills the full base URL and model", () => {
  const presets = [{ id: "commandcode", api_base: "https://api.commandcode.ai/provider/v1",
                     model: "openai/deepseek/deepseek-v4.1-flash" }];
  assert.deepEqual(logic.presetFill(presets, "commandcode"),
    { api_base: "https://api.commandcode.ai/provider/v1",
      model: "openai/deepseek/deepseek-v4.1-flash" });
  assert.deepEqual(logic.presetFill(presets, "custom"), { api_base: "", model: "" });
});

test("request state walks idle -> running -> ok | error | timeout", () => {
  let state = logic.requestState(undefined, { type: "start", message: "testing..." });
  assert.equal(state.status, "running");
  assert.equal(logic.isBusy(state), true);

  state = logic.requestState(state, { type: "ok", rows: [{ path: "a" }, { path: "b" }] });
  assert.equal(state.status, "ok");
  assert.equal(state.message, "2 path(s) checked");
  assert.equal(logic.isBusy(state), false);

  state = logic.requestState(state, { type: "error", message: "no key stored" });
  assert.equal(state.status, "error");
  assert.equal(state.message, "no key stored");

  state = logic.requestState(state, { type: "timeout" });
  assert.equal(state.status, "timeout");
  assert.equal(state.message, "taking too long; check the crew server");

  state = logic.requestState(state, { type: "reset" });
  assert.equal(state.status, "idle");
});

/* --- diagram ------------------------------------------------------------- */

test("each pattern selects its own variant", () => {
  assert.equal(logic.diagramFor("parallel").pattern, "parallel");
  assert.equal(logic.diagramFor("nonsense").pattern, "sequential");
  assert.equal(logic.diagramFor("p2p").blackboardAuditOnly, true);
  assert.equal(logic.diagramFor("sequential").blackboardAuditOnly, false);

  const parallel = logic.diagramFor("parallel").edges.map((edge) => edge.from + "->" + edge.to);
  assert.ok(parallel.includes("planner->web_researcher"));
  assert.ok(parallel.includes("calc_researcher->calc_tool"));
  assert.ok(logic.diagramFor("hierarchical").edges.some((edge) => edge.label === "revision"));
});

test("composer pattern values cover exactly the supported set", () => {
  assert.deepEqual(logic.PATTERNS, ["sequential", "parallel", "hierarchical", "p2p"]);
  assert.equal(logic.DEFAULT_PATTERN, "hierarchical");
  for (const pattern of logic.PATTERNS) {
    assert.equal(logic.patternValue(pattern), pattern);
  }
  assert.equal(logic.patternValue("nonsense"), "hierarchical");
  assert.equal(logic.patternValue(undefined), "hierarchical");
});

test("run ids are accepted only when they are non-empty strings", () => {
  assert.equal(logic.validRunId("r1"), true);
  assert.equal(logic.validRunId("  run-123  "), true);
  assert.equal(logic.validRunId(""), false);
  assert.equal(logic.validRunId("   "), false);
  assert.equal(logic.validRunId(null), false);
  assert.equal(logic.validRunId(undefined), false);
  assert.equal(logic.validRunId({ run_id: "r1" }), false);
  assert.equal(logic.validRunId(7), false);
});

test("metric rows use only recorded values and mark missing fields", () => {
  assert.deepEqual(logic.metricRows([]), []);
  assert.deepEqual(logic.metricRows(null), []);

  const rows = logic.metricRows([
    {
      agent: "researcher", framework: "adk", stage_ms: 1500,
      model_calls: 3, tokens_in: 100, tokens_out: 20, reasoning_tokens: 5,
      tool_calls: 4, errors: 0, a2a_bytes: 12, evidence_items: 7,
    },
    { agent: "critic", stage_ms: "bad", model_calls: null },
    null,
    "not a row",
  ]);

  assert.equal(rows.length, 2);
  assert.equal(rows[0].stage, "1.5 s");
  assert.equal(rows[0].tokens_in, "100");
  assert.equal(rows[1].agent, "critic");
  assert.equal(rows[1].stage, "\u2014");
  assert.equal(rows[1].models, "\u2014");
  assert.equal(rows[1].framework, "\u2014");
});

test("critic diagnostics distinguish rejection, failure, and missing verdicts", () => {
  const rejected = logic.critiqueDiagnostic({
    value: { issues: ["missing impact"], severity: "high", approved: false },
  });
  assert.equal(rejected.status, "rejected");
  assert.equal(rejected.issues[0], "missing impact");
  assert.match(rejected.label, /rejected/);

  const missing = logic.critiqueDiagnostic(null);
  assert.equal(missing.status, "missing");
  assert.match(missing.reason, /not an approval/);

  const approved = logic.critiqueDiagnostic({ approved: true, severity: "low", issues: [] });
  assert.equal(approved.status, "approved");
  assert.equal(approved.label, "critic approved (low)");
});

test("node states follow the events, including failure", () => {
  const state = logic.diagramState(events, "sequential");

  assert.equal(state.nodes.planner.state, "done");
  assert.equal(state.nodes.planner.label, "1.2 s");
  assert.equal(state.nodes.critic.state, "failed");
  assert.equal(state.nodes.rag_tool.calls, 1);
});

test("a2a bytes reach the edge label", () => {
  const state = logic.diagramState(events, "sequential");
  assert.equal(state.edges["supervisor->writer"].bytes, "156 B out / 90 B in");
  assert.equal(state.edges["supervisor->writer"].active, true);
  assert.deepEqual(state.a2a, { out: 156, in: 90 });
});

test("replay and live produce the same diagram state", () => {
  const live = logic.diagramState(events, "p2p");
  const replay = logic.diagramState(JSON.parse(JSON.stringify(events)), "p2p");
  assert.deepEqual(live, replay);
});

test("an unknown pattern still renders a diagram", () => {
  const state = logic.diagramState([], "deep");
  assert.equal(state.pattern, "sequential");
  assert.equal(Object.keys(state.nodes).length, logic.NODES.length);
});

/* --- declutter helpers --------------------------------------------------- */

test("relative time reads naturally", () => {
  const now = "2026-10-28T12:00:00Z";
  assert.equal(logic.relativeTime("2026-10-28T11:59:30Z", now), "just now");
  assert.equal(logic.relativeTime("2026-10-28T11:55:00Z", now), "5 m ago");
  assert.equal(logic.relativeTime("2026-10-28T10:00:00Z", now), "2 h ago");
  assert.equal(logic.relativeTime("2026-10-27T11:00:00Z", now), "yesterday");
  assert.equal(logic.relativeTime("2026-10-25T11:00:00Z", now), "3 d ago");
  assert.equal(logic.relativeTime("", now), "");
});

test("history groups by day and collapses consecutive duplicates", () => {
  const runs = [
    { run_id: "a", question: "same question", pattern: "sequential", approved: true,
      started_at: "2026-10-28T10:00:00Z" },
    { run_id: "b", question: "same question", pattern: "sequential", approved: true,
      started_at: "2026-10-28T09:00:00Z" },
    { run_id: "c", question: "same question", pattern: "sequential", approved: true,
      started_at: "2026-10-28T08:00:00Z" },
    { run_id: "d", question: "another question", pattern: "p2p", approved: false,
      started_at: "2026-10-27T08:00:00Z" },
    { run_id: "e", question: "old question", pattern: "parallel", approved: null,
      started_at: "2026-10-20T08:00:00Z" },
  ];
  const grouped = logic.groupHistory(runs, { now: "2026-10-28T12:00:00Z" });

  assert.deepEqual(grouped.groups.map((g) => g.label), ["Today", "Yesterday", "Earlier"]);
  assert.equal(grouped.groups[0].items.length, 1, "consecutive duplicates collapse");
  assert.equal(grouped.groups[0].items[0].count, 3);
  assert.equal(grouped.groups[1].items[0].count, 1);
  assert.equal(grouped.hidden, 0);
});

test("history shows eight rows and reports the rest as hidden", () => {
  const runs = Array.from({ length: 11 }, (_, i) => ({
    run_id: "r" + i, question: "question " + i, pattern: "sequential",
    started_at: new Date(Date.parse("2026-10-28T11:00:00Z") - i * 60000).toISOString(),
  }));
  const grouped = logic.groupHistory(runs, { now: "2026-10-28T12:00:00Z" });

  const shown = grouped.groups.reduce((total, group) => total + group.items.length, 0);
  assert.equal(shown, 8);
  assert.equal(grouped.hidden, 3);
});

test("the tab reducer opens activity while running and answers when done", () => {
  let state = logic.tabReducer(undefined, { type: "select", tab: "blackboard" });
  assert.equal(state.tab, "blackboard");
  assert.equal(state.running, false);

  state = logic.tabReducer(state, { type: "start" });
  assert.equal(state.tab, "activity");
  assert.equal(state.running, true);

  state = logic.tabReducer(state, { type: "select", tab: "metrics" });
  assert.equal(state.tab, "metrics");
  assert.equal(state.running, true, "selecting does not end the run");

  state = logic.tabReducer(state, { type: "finish" });
  assert.equal(state.tab, "answer");
  assert.equal(state.running, false);
});

test("the progress strip names each agent and its state", () => {
  const strip = logic.progressStrip([
    { kind: "stage_end", agent: "planner", duration_ms: 10 },
    { kind: "stage_start", agent: "researcher" },
  ]);
  assert.equal(strip, "planner done \u00b7 researcher running");
});

test("the status pill is short with a full tooltip", () => {
  const pill = logic.statusPill({ crew_server: "up", provider_configured: true,
    model: "openai/deepseek/deepseek-v4.1-flash", api_host: "gateway.example" }, false);
  assert.equal(pill.short, "crew up - live - deepseek-v4.1-flas - gateway.example");
  assert.ok(pill.tooltip.includes("gateway.example"));

  const replay = logic.statusPill({ crew_server: "up", provider_configured: false }, false);
  assert.equal(replay.short, "crew up - replay");
});

/* ------------------------------------------------------ response context tests */

test("chat context distinguishes RAG off, passages, empty and failure", () => {
  const base = {
    role: "assistant", id: "m1", status: "ok", content: "answer",
    provider: "openai", model: "test", capabilities: [], tools: [], sources: [],
    tool_events: [], usage: null, meta: {},
  };
  const off = logic.chatContextModel({ message: base, userMessage: { content: "q" } });
  assert.equal(off.rag.state, "off");
  assert.equal(off.context.prompt, "q");
  assert.ok(off.context.notes.some((note) => /upload/i.test(note)));

  const passages = logic.chatContextModel({
    message: {
      ...base,
      capabilities: ["rag"],
      sources: [{ kind: "document", index: 1, document: "handbook.pdf", page: 4, snippet: "excerpt" }],
      meta: { trace: [{ id: "retrieval-rag_search", stage: "retrieval", tool: "rag_search", label: "Document search", status: "ok", summary: "1 passage(s) used" }] },
    },
    userMessage: { content: "q" },
  });
  assert.equal(passages.rag.state, "passages");
  assert.equal(passages.rag.count, 1);
  assert.equal(passages.sources[0].title, "handbook.pdf");
  assert.equal(passages.sources[0].badges[0], "Retrieved");

  const empty = logic.chatContextModel({
    message: {
      ...base,
      capabilities: ["rag"],
      meta: { trace: [{ id: "retrieval-rag_search", stage: "retrieval", tool: "rag_search", label: "Document search", status: "ok", summary: "0 passage(s) used" }] },
    },
    userMessage: { content: "q" },
  });
  assert.equal(empty.rag.state, "empty");

  const failed = logic.chatContextModel({
    message: {
      ...base,
      capabilities: ["rag"],
      meta: { trace: [{ id: "retrieval-rag_search", stage: "retrieval", tool: "rag_search", label: "Document search", status: "error", error: "offline" }] },
    },
    userMessage: { content: "q" },
  });
  assert.equal(failed.rag.state, "failed");
});

test("chat metadata row reflects real response state and actions", () => {
  const model = logic.chatContextModel({
    message: {
      role: "assistant", id: "m1", status: "ok", content: "answer",
      provider: "openai", model: "test", capabilities: [],
      sources: [{ kind: "web", index: 1, title: "Docs", url: "https://example.com/docs", snippet: "s" }],
      tools: [], tool_events: [], usage: { tokens_in: 10, tokens_out: 4 }, meta: {},
    },
    userMessage: { content: "q" },
  });
  const meta = logic.responseMetaFromModel(model);
  assert.deepEqual(meta.map((item) => item.id), ["status", "rag", "sources", "context"]);
  assert.equal(meta[0].label, "Completed");
  assert.equal(meta[1].label, "RAG off");
  assert.equal(meta[2].label, "Sources 1");
});

test("research context identifies fetched pages, snippets and cited sources", () => {
  const snapshot = {
    task: { value: { question: "What changed?", pattern: "parallel", tools: ["rag_search", "web_search", "fetch_page"] } },
    tool_report: {
      enabled: ["rag_search", "web_search", "fetch_page"],
      planned: ["rag_search", "web_search", "fetch_page"],
      planned_blocked: [],
      invoked: ["fetch_page", "web_search"],
      failed: [],
      skipped: [],
      calls: [{ tool: "fetch_page", ok: true }],
    },
    evidence: [
      { id: "fetch-1", source_type: "web", source_ref: "https://docs.example/release", retrieved_by: "web_researcher", score: null, content: "Release notes\nA fetched page body" },
      { id: "web-1", source_type: "web", source_ref: "https://search.example/result", retrieved_by: "web_researcher", score: null, content: "Search snippet" },
      { id: "rag-1", source_type: "rag", source_ref: "handbook.pdf#p4-chunk-2", retrieved_by: "rag_researcher", score: 0.7, content: "Document passage" },
    ],
    final_report: { value: { citations: [{ source_ref: "https://docs.example/release" }], confidence: 0.8 } },
  };
  const model = logic.researchContextModel({
    runId: "run-1234", phase: "history", summary: { pattern: "parallel", outcome: "approved", evidence_sufficient: true },
    snapshot, events: [], pattern: "parallel",
  });

  assert.equal(model.kind, "research");
  assert.equal(model.rag.state, "passages");
  assert.equal(model.rag.count, 1);
  assert.equal(model.citationCount, 1);
  assert.equal(model.sources.find((source) => source.id === "research-source-fetch-1").badges.includes("Page fetched"), true);
  assert.equal(model.sources.find((source) => source.id === "research-source-fetch-1").badges.includes("Cited in report"), true);
  assert.equal(model.sources.find((source) => source.id === "research-source-web-1").badges.includes("Search snippet"), true);
  assert.equal(model.sources.find((source) => source.id === "research-source-rag-1").badges.includes("Document passage"), true);
  assert.equal(model.run.canOpenDetails, true);
  assert.equal(model.status.label, "Completed");
});

test("research sources accept the snapshot text field and source type", () => {
  const model = logic.researchContextModel({
    runId: "r1",
    phase: "history",
    summary: { pattern: "sequential" },
    snapshot: {
      task: { value: { question: "q", pattern: "sequential", tools: ["rag_search"] } },
      tool_report: { enabled: ["rag_search"], invoked: ["rag_search"], failed: [], calls: [{ tool: "rag_search", ok: true }], skipped: [] },
      evidence: [{
        id: "doc_1", source_type: "rag", source_ref: "handbook.pdf#p7-chunk-3",
        retrieved_by: "rag_researcher", score: 0.8,
        text: "The snapshot text body",
      }],
    },
    events: [],
  });

  assert.equal(model.rag.state, "passages");
  assert.equal(model.rag.count, 1);
  assert.equal(model.sources[0].type, "Document passage");
  assert.match(model.sources[0].excerpt, /snapshot text body/);
});

test("research context reports disabled, pending and failed RAG accurately", () => {
  const base = {
    runId: "r1",
    phase: "history",
    summary: { pattern: "sequential" },
    snapshot: {
      task: { value: { question: "q", pattern: "sequential", tools: ["web_search"] } },
      tool_report: { enabled: ["web_search"], invoked: [], failed: [], calls: [], skipped: [] },
      evidence: [],
    },
    events: [],
  };
  assert.equal(logic.researchContextModel(base).rag.state, "off");

  const pending = {
    ...base,
    phase: "running",
    snapshot: {
      task: { value: { question: "q", pattern: "sequential", tools: ["rag_search"] } },
      tool_report: { enabled: ["rag_search"], invoked: [], failed: [], calls: [], skipped: [] },
      evidence: [],
    },
  };
  assert.equal(logic.researchContextModel(pending).rag.state, "pending");

  const failed = {
    ...base,
    snapshot: {
      task: { value: { question: "q", pattern: "sequential", tools: ["rag_search"] } },
      tool_report: { enabled: ["rag_search"], invoked: ["rag_search"], failed: ["rag_search"], calls: [{ tool: "rag_search", ok: false, error: "offline" }], skipped: [] },
      evidence: [],
    },
  };
  assert.equal(logic.researchContextModel(failed).rag.state, "failed");
});

test("research status and response metadata do not claim success when incomplete", () => {
  const model = logic.researchContextModel({
    runId: "r1",
    phase: "history",
    summary: {
      pattern: "hierarchical",
      outcome: "incomplete",
      evidence_sufficient: false,
      critic_status: "rejected",
      critique_reason: "missing fetched page",
      unmet_requirements: ["no successful fetch"],
    },
    snapshot: {
      task: { value: { question: "q", pattern: "hierarchical", tools: [] } },
      tool_report: { enabled: [], invoked: [], failed: ["fetch_page"], calls: [], skipped: [] },
      evidence: [],
    },
    events: [],
  });
  assert.equal(model.status.label, "Incomplete");
  const meta = logic.responseMetaFromModel(model);
  assert.equal(meta[0].label, "Incomplete");
  assert.ok(meta.some((item) => item.id === "warnings" && /gap/.test(item.label)));
  assert.equal(logic.safeHttpUrl("javascript:alert(1)"), "");
});

/* ------------------------------------------------------------ chat tests */

test("the SSE parser handles frames split across chunks", () => {
  const parser = logic.sseParser();
  assert.deepEqual(parser.push('id: 1\nevent: delta\ndata: {"text":"a"}'), []);
  assert.deepEqual(parser.push("\n\n"), [
    { event: "delta", id: "1", data: '{"text":"a"}' },
  ]);
  assert.deepEqual(parser.flush(), []);
});

test("the SSE parser joins data lines, ignores comments and handles CRLF", () => {
  const parser = logic.sseParser();
  const frames = parser.push(": keep-alive\r\n\r\nevent: tool\r\ndata: one\r\ndata: two\r\n\r\n");
  assert.deepEqual(frames, [{ event: "tool", id: "", data: "one\ntwo" }]);
});

test("the SSE parser flushes a final unterminated frame", () => {
  const parser = logic.sseParser();
  assert.deepEqual(parser.push("data: tail"), []);
  assert.deepEqual(parser.flush(), [{ event: "message", id: "", data: "tail" }]);
});

test("usage and tool labels only show real values", () => {
  assert.equal(logic.usageLabel(null), "");
  assert.equal(logic.usageLabel({ tokens_in: 0, tokens_out: 0 }), "");
  assert.equal(logic.usageLabel({ tokens_in: 1200, tokens_out: 30, reasoning_tokens: 4 }),
    "1,200 in \u00b7 30 out \u00b7 4 reasoning");
  assert.equal(logic.toolSummary([]), "");
  assert.equal(logic.toolSummary([{ status: "ok" }]), "1 tool \u00b7 1 ok");
  assert.equal(logic.toolSummary([{ status: "ok" }, { status: "error" }]),
    "2 tools \u00b7 1 failed");
});

test("source labels distinguish documents and web pages", () => {
  assert.equal(logic.sourceLabel({ kind: "document", document: "handbook.pdf", page: 12 }),
    "handbook.pdf \u00b7 p12");
  assert.equal(logic.sourceLabel({ kind: "web", url: "https://docs.python.org/3.13/" }),
    "docs.python.org");
});

test("settings filtering and focus wrapping are deterministic", () => {
  const sections = [
    { id: "models", label: "Models", keywords: ["provider"] },
    { id: "library", label: "Library", keywords: ["rag"] },
  ];
  assert.deepEqual(logic.filterSettingsSections(sections, ""), ["models", "library"]);
  assert.deepEqual(logic.filterSettingsSections(sections, "rag"), ["library"]);
  assert.equal(logic.nextFocusIndex(3, 2, false), 0);
  assert.equal(logic.nextFocusIndex(3, 0, true), 2);
  assert.equal(logic.nextFocusIndex(0, 0, false), 0);
});

test("Capillary options keep the requested values and do not fake bayer4", () => {
  const options = logic.capillaryOptions(null);
  assert.equal(options.palette, "var(--capillary-ink, #f2f0ea)");
  assert.equal(options.speed, 1);
  assert.equal(options.cellSize, 10);
  assert.equal(options.opacity, 1);
  assert.equal(options.charset, "detailed");
  assert.equal(options.cursor, "lantern");
  assert.equal(options.mark, "glyph");
  assert.equal(options.dither, 0);
  assert.equal(options.text, "ASCIIFY");
  assert.deepEqual(options.look, { scene: "flow" });
  assert.equal(options.algorithm, undefined);
  assert.equal(logic.CAPILLARY_REQUESTED.algorithm, "bayer4");
  assert.equal(logic.CAPILLARY_REQUESTED.ink, "#f2f0ea");
});

test("tutorial placement stays inside the viewport", () => {
  const target = { left: 500, top: 400, width: 120, height: 30 };
  const card = { width: 300, height: 200 };
  const viewport = { width: 1200, height: 800 };
  const below = logic.tutorialPlacement(target, card, viewport, "bottom");
  assert.equal(below.placement, "bottom");
  assert.equal(below.top, 442);
  assert.equal(below.left, 410);

  const above = logic.tutorialPlacement(target, card, viewport, "top");
  assert.equal(above.placement, "top");
  assert.equal(above.top, 188);

  const right = logic.tutorialPlacement(target, card, viewport, "right");
  assert.equal(right.placement, "right");
  assert.equal(right.left, 632);

  const clamped = logic.tutorialPlacement(
    { left: 1150, top: 20, width: 20, height: 20 }, card, viewport, "bottom"
  );
  assert.equal(clamped.left, 888);
  assert.equal(logic.tutorialPlacement(target, card, { width: 390, height: 844 }, "bottom").mobile, true);
});

test("tutorial persistence and auto-start rules are pure", () => {
  assert.equal(logic.tutorialProgress(1, 8), "Step 2 of 8");
  assert.deepEqual(logic.parseTutorialState('{"status":"done","step":3}'),
    { status: "done", step: 3 });
  assert.equal(logic.parseTutorialState("not json"), null);
  assert.equal(logic.parseTutorialState('{"status":"open"}'), null);
  assert.equal(logic.shouldAutoStartTutorial({ view: "run", params: {} }, null, false), true);
  assert.equal(logic.shouldAutoStartTutorial({ view: "run", params: {} }, { status: "done" }, false), false);
  assert.equal(logic.shouldAutoStartTutorial({ view: "run", params: {} }, null, true), false);
  assert.equal(logic.shouldAutoStartTutorial({ view: "settings", params: {} }, null, false), false);
  assert.equal(logic.shouldAutoStartTutorial({ view: "replay=x", params: {} }, null, false), false);
});

/* ------------------------------------------------------ tool selection tests */

const TOOL_IDS = ["rag_search", "web_search", "fetch_page", "calculate", "forecast"];

test("all tools is the default and selecting an individual clears it", () => {
  assert.equal(logic.normaliseToolSelection(null, TOOL_IDS), null);
  assert.equal(logic.toolSelectionLabel(null), "All tools");

  const one = logic.toggleToolSelection(null, "web_search", TOOL_IDS);
  assert.deepEqual(one, ["web_search"]);
  assert.equal(logic.toolSelectionLabel(one), "1 tool");
});

test("multiple tools can be selected and deselecting the last is explicit", () => {
  let selected = logic.toggleToolSelection(null, "web_search", TOOL_IDS);
  selected = logic.toggleToolSelection(selected, "fetch_page", TOOL_IDS);
  assert.deepEqual(selected, ["web_search", "fetch_page"]);
  assert.equal(logic.toolSelectionLabel(selected), "2 tools");

  selected = logic.toggleToolSelection(selected, "web_search", TOOL_IDS);
  selected = logic.toggleToolSelection(selected, "fetch_page", TOOL_IDS);
  assert.deepEqual(selected, []);
  assert.equal(logic.toolSelectionLabel(selected), "No tools");
  assert.equal(logic.toolSelectionLabel(null), "All tools");
});

test("selecting every individual tool normalises to all tools", () => {
  let selected = null;
  TOOL_IDS.forEach((id) => { selected = logic.toggleToolSelection(selected, id, TOOL_IDS); });
  assert.equal(selected, null);
  assert.equal(logic.toggleToolSelection(["rag_search"], "__all__", TOOL_IDS), null);
});

/* --------------------------------------------------------- activity trace tests */

test("research trace derives agent, tool, skip and failure rows", () => {
  const trace = logic.researchTrace([
    { event_id: "1", seq: 1, kind: "stage_start", agent: "planner", framework: "adk", ts: "2026-01-01T00:00:00Z" },
    { event_id: "2", seq: 2, kind: "stage_end", agent: "planner", framework: "adk", duration_ms: 1200, ts: "2026-01-01T00:00:01Z" },
    { event_id: "3", seq: 3, kind: "stage_start", agent: "web_researcher", framework: "adk", ts: "2026-01-01T00:00:02Z" },
    { event_id: "4", seq: 4, kind: "tool_call", agent: "web_researcher", payload_summary: "fetch_page: https://example.com", ok: true, duration_ms: 800, ts: "2026-01-01T00:00:03Z", extra: { sources: [{ title: "Example page", url: "https://example.com" }] } },
    { event_id: "5", seq: 5, kind: "skip", agent: "rag_researcher", payload_summary: "rag_researcher skipped: no plan step", framework: "adk", ts: "2026-01-01T00:00:03Z" },
    { event_id: "6", seq: 6, kind: "error", agent: "critic", error_type: "Timeout", error_message: "critic timed out", framework: "crewai", ts: "2026-01-01T00:00:04Z" },
  ], { finished: true });

  assert.equal(trace.rows.length, 5);
  assert.equal(trace.rows[0].status, "done");
  assert.equal(trace.rows[2].label, "fetch_page");
  assert.ok(trace.rows[2].detail.includes("Example page"));
  assert.equal(trace.rows[3].status, "skipped");
  assert.equal(trace.rows[4].status, "failed");
  assert.equal(trace.rows[4].framework, "crewai");
  assert.equal(trace.failed, true);
  assert.equal(trace.elapsed_ms, 4000);
});

test("an empty research trace is explicit rather than fabricated", () => {
  const trace = logic.researchTrace([], { finished: true });
  assert.deepEqual(trace.rows, []);
  assert.equal(trace.running, false);
  assert.equal(trace.failed, false);
  assert.equal(trace.summary, "No activity recorded");
  assert.equal(trace.current, null);
});

test("wrapper and remote A2A stages are distinct and both complete", () => {
  const trace = logic.researchTrace([
    { event_id: "s1", seq: 1, kind: "stage_start", agent: "analyst", framework: "adk", extra: { stage_scope: "adk-wrapper" } },
    { event_id: "s2", seq: 2, kind: "stage_start", agent: "analyst", framework: "crewai", extra: { stage_scope: "crewai-remote" } },
    { event_id: "m1", seq: 3, kind: "model_call", agent: "analyst", framework: "crewai", tokens_in: 20, tokens_out: 8 },
    { event_id: "s3", seq: 4, kind: "stage_end", agent: "analyst", framework: "crewai", duration_ms: 900, tokens_in: 20, tokens_out: 8 },
    { event_id: "a1", seq: 5, kind: "a2a_call", agent: "analyst", framework: "adk", duration_ms: 1000, extra: { request_bytes: 156, response_bytes: 146, handoff: "adk->crewai" } },
    { event_id: "s4", seq: 6, kind: "stage_end", agent: "analyst", framework: "adk", duration_ms: 1000 },
  ], { finished: true });

  const stages = trace.rows.filter((row) => row.agent === "analyst");
  assert.equal(stages.length, 3, "two stage rows plus the A2A handoff");
  assert.equal(stages[0].label, "Analyst (handoff)");
  assert.equal(stages[0].status, "done");
  assert.equal(stages[1].label, "Analyst (remote)");
  assert.equal(stages[1].status, "done");
  assert.equal(stages[2].label, "A2A handoff");
  assert.equal(trace.rows.some((row) => row.status === "running"), false);
});

test("supervisor duration survives a summary stage_end and tokens stay unavailable", () => {
  const rows = logic.reduceTimeline([
    { kind: "stage_start", agent: "supervisor", framework: "adk" },
    { kind: "stage_end", agent: "supervisor", framework: "adk", duration_ms: 160901.41, tokens_in: null, tokens_out: null },
    { kind: "stage_end", agent: "supervisor", framework: "adk", duration_ms: null, tokens_in: null, tokens_out: null, payload_summary: "run_summary hierarchical" },
  ]);
  const supervisor = rows[0];
  assert.equal(supervisor.ms, 160901);
  assert.equal(supervisor.tokens_known, false);
  assert.equal(logic.tokens(supervisor.tokens_in, supervisor.tokens_out, supervisor.tokens_known), "tokens unavailable");
});

test("research trace renders a recovery decision without inventing execution", () => {
  const trace = logic.researchTrace([
    { event_id: "r1", seq: 1, kind: "recovery", agent: "supervisor", framework: "adk", payload_summary: "research recovery skipped: existing evidence is sufficient", extra: { skipped: true, reason: "existing evidence is sufficient" } },
    { event_id: "r2", seq: 2, kind: "recovery", agent: "supervisor", framework: "adk", payload_summary: "research recovery: web_researcher missing evidence", extra: { skipped: false, role: "web_researcher", reason: "missing evidence" } },
  ], { finished: true });

  assert.equal(trace.rows[0].label, "Recovery skipped");
  assert.equal(trace.rows[0].status, "skipped");
  assert.equal(trace.rows[1].label, "Research recovery");
  assert.equal(trace.rows[1].status, "done");
});

test("research trace carries the recorded verdict, revisions and evidence gaps", () => {
  const trace = logic.researchTrace([], {
    finished: true,
    summary: {
      critic_status: "rejected",
      critique_reason: "missing source evidence",
      revision_rounds: 1,
      evidence_sufficient: false,
      unmet_requirements: ["no successful fetch of the requested URL"],
    },
  });

  assert.equal(trace.rows[0].label, "Verdict");
  assert.equal(trace.rows[0].status, "failed");
  assert.ok(trace.rows[0].detail.includes("missing source evidence"));
  assert.equal(trace.rows[1].label, "Revisions");
  assert.equal(trace.rows[1].detail, "1 revision round");
  assert.equal(trace.rows[2].label, "Evidence gap");
  assert.equal(trace.rows[2].status, "failed");
});

test("chat trace maps running, tool and done events without inventing stages", () => {
  const trace = logic.chatTrace([
    { id: "turn-start", stage: "start", label: "Preparing response", status: "ok" },
    { id: "retrieval-rag_search", stage: "retrieval", label: "Searching documents", status: "running", tool: "rag_search" },
    { id: "retrieval-rag_search", stage: "retrieval", label: "Document search", status: "ok", tool: "rag_search", summary: "2 passages used", elapsed_ms: 120 },
    { id: "call-1", stage: "tool", label: "Calculator", status: "ok", tool: "calculate", summary: "= 42", elapsed_ms: 5 },
    { id: "turn-done", stage: "done", label: "Completed", status: "ok", elapsed_ms: 500 },
  ]);
  assert.equal(trace.rows.length, 4, "the duplicate retrieval event is merged");
  assert.equal(trace.rows[1].label, "Document search");
  assert.equal(trace.rows[1].detail, "2 passages used");
  assert.equal(trace.rows[1].tool, "rag_search");
  assert.equal(trace.rows[3].label, "Completed");
  assert.equal(trace.running, false);
  assert.equal(trace.elapsed_ms, 500);
});