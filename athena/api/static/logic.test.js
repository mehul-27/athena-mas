/* Node tests for static/logic.js. Run: node --test athena/api/static/logic.test.js */
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
];

test("timeline derives done rows with duration and tokens", () => {
  const rows = logic.reduceTimeline(events);
  const by = Object.fromEntries(rows.map((row) => [row.agent, row]));

  assert.equal(by.planner.status, "done");
  assert.equal(by.planner.ms, 1235);
  assert.equal(by.planner.tokens_in, 10);
  assert.equal(by.writer.status, "done");
  assert.equal(by.writer.framework, "crewai");
  assert.equal(by.critic.status, "failed");
  assert.equal(rows.some((row) => row.status === "running"), false);
});

test("replay rows never stay running", () => {
  const rows = logic.reduceTimeline([
    { kind: "stage_start", agent: "analyst" },
    { kind: "stage_end", agent: "analyst", duration_ms: 5 },
  ]);
  assert.equal(rows[0].status, "done");
});

test("a2a bytes add up", () => {
  assert.deepEqual(logic.a2aBytes(events), { out: 156, in: 90 });
});

test("verdict label never renders an element", () => {
  assert.equal(logic.verdictLabel(true, "low"), "critic approved (low)");
  assert.equal(logic.verdictLabel(false, "high"), "critic rejected (high)");
  assert.equal(logic.verdictLabel(null, null), "critic unknown");
  assert.equal(logic.verdictLabel(true, null), "critic approved");
});

test("citation rows always carry a quote", () => {
  const rows = logic.citationRows({ citations: [
    { source_ref: "nasa.pdf#p1", quote: "systems engineering is..." },
    { source_ref: "doc#p2" },
  ] });
  assert.equal(rows.length, 2);
  assert.equal(rows[0].quote, "systems engineering is...");
  assert.equal(rows[1].quote, "(quote not recorded)");
});

test("status strip says replay when no provider", () => {
  const strip = logic.statusStrip({ crew_server: "down", provider_configured: false }, false);
  assert.equal(strip.mode, "replay");
  assert.equal(strip.model, "n/a (replay)");

  const live = logic.statusStrip({ crew_server: "up", provider_configured: true,
    model: "openai/x", api_host: "gateway.local" }, false);
  assert.equal(live.mode, "live");
  assert.equal(live.host, "gateway.local");
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
  assert.equal(byName.plan.lines[0], "1. search -> rag_researcher");
  assert.equal(byName.evidence.table, true);
  assert.equal(byName.evidence.rows[0][3].length, 200);
  assert.equal(byName.critique.verdict, "critic rejected (high)");
});

test("missing sections produce no cards", () => {
  assert.deepEqual(logic.blackboardCards({}), []);
});
