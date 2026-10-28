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