/* Node tests for static/telemetry.js.
 * Run: node athena/api/static/telemetry.test.js
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const T = require("./telemetry.js");

const payload = {
  run_id: "run-1",
  exists: true,
  pattern: "sequential",
  agents: [
    {
      agent: "planner", framework: "adk", start_ms: 0, end_ms: 1600, duration_ms: 1600,
      tokens_in: 1000, tokens_out: 200, reasoning_tokens: 50, cost_usd: 0.000748,
      cost_basis: "list_price", model_calls: 1, tool_calls: 0, tool_errors: 0,
      errors: 0, retries: 0, retries_inferred: 0, guardrails: 0, handoffs: 0,
      recoveries: 0, limiter_wait_ms: 120, a2a_bytes: 0,
      evidence_contributions: { count: 0, source: "none" },
      terminal_state: "succeeded", models: ["llama-3.3-70b-versatile"],
    },
    {
      agent: "writer", framework: "crewai", start_ms: 2000, end_ms: 2600, duration_ms: 600,
      tokens_in: 0, tokens_out: 0, cost_usd: null, cost_basis: "no_model_calls",
      model_calls: 0, tool_calls: 0, errors: 1, retries: 0, retries_inferred: 1,
      a2a_bytes: 236, evidence_contributions: { count: 2, source: "blackboard" },
      terminal_state: "failed",
    },
  ],
  spans: [
    { span_id: "s1", agent: "planner", kind: "stage", label: "planner stage", start_offset_ms: 0, end_offset_ms: 1600, duration_ms: 1600, ok: true },
    { span_id: "s2", agent: "planner", kind: "tool_call", label: "tool: fetch_page", start_offset_ms: 600, end_offset_ms: 900, duration_ms: 300, ok: false },
    { span_id: "s3", agent: "writer", kind: "a2a_call", label: "a2a: writer", start_offset_ms: 2000, end_offset_ms: 2400, duration_ms: 400, ok: true },
    { span_id: "s4", agent: "writer", kind: "stage", label: "writer stage (open)", start_offset_ms: 2500, end_offset_ms: null, duration_ms: null, open: true },
  ],
  totals: {
    wall_ms: 4000, tokens_in: 1000, tokens_out: 200, cost_usd: 0.000748,
    cost_basis: "list_price", unpriced_calls: 1, events: 14, terminal_state: "approved",
    tool_calls: 2, retries: 0, retries_inferred: 1, guardrails: 0, handoffs: 1,
  },
  cost_basis: { as_of: "2026-11-06", currency: "USD", note: "list prices" },
  missing: ["no evidence section on the blackboard"],
  notes: ["every value is derived from the stored event log and blackboard"],
  reason: "",
};

const heat = {
  run_id: "run-1",
  agents: ["planner", "writer"],
  metrics: [
    { name: "tokens_in", label: "tokens in", unit: "tokens" },
    { name: "cost_usd", label: "cost (USD)", unit: "usd" },
  ],
  metric_matrix: {
    planner: {
      tokens_in: { raw: 1000, normalised: 1, unit: "tokens", event_ids: ["e1"] },
      cost_usd: { raw: 0.000748, normalised: 1, unit: "usd", event_ids: ["e1"] },
    },
    writer: {
      tokens_in: { raw: null, normalised: null, unit: "tokens", event_ids: [] },
      cost_usd: { raw: null, normalised: null, unit: "usd", event_ids: [] },
    },
  },
  metric_normalisation: {
    basis: "min-max per metric column, across the agents of this run",
    ranges: { tokens_in: { min: 0, max: 1000 }, cost_usd: { min: 0, max: 0.000748 } },
  },
  buckets: [
    { index: 0, label: "1. planner", agent: "planner", start_offset_ms: 0, end_offset_ms: 1600, duration_ms: 1600 },
    { index: 1, label: "2. writer", agent: "writer", start_offset_ms: 2000, end_offset_ms: 2600, duration_ms: 600 },
  ],
  bucket_matrix: {
    planner: { "0": { raw: 1600, normalised: 1, unit: "ms", event_ids: ["s1"] }, "1": { raw: 0, normalised: 0, unit: "ms", event_ids: [] } },
    writer: { "0": { raw: 0, normalised: 0, unit: "ms", event_ids: [] }, "1": { raw: 600, normalised: 1, unit: "ms", event_ids: ["s3"] } },
  },
  bucket_normalisation: { basis: "min-max per stage column, across the agents of this run" },
  png_fallback: "/api/runs/run-1/heatmap.png",
  reason: "",
};

test("shaping keeps the recorded numbers and states what is missing", () => {
  const shape = T.shapeTelemetry(payload);

  assert.equal(shape.ok, true);
  assert.equal(shape.agents.length, 2);
  const planner = shape.agents[0];
  assert.equal(planner.tokensIn, 1000);
  assert.equal(planner.costUsd, 0.000748);
  assert.equal(planner.limiterWait, 120);
  assert.equal(planner.terminalState, "succeeded");
  assert.equal(shape.agents[1].costUsd, null, "an unpriced cost stays null");
  assert.equal(shape.agents[1].evidence, 2);
  assert.equal(shape.totals.wallMs, 4000);
  assert.equal(shape.totals.costUsd, 0.000748);
  assert.deepEqual(shape.missing, ["no evidence section on the blackboard"]);
});

test("shaping tolerates a missing or malformed payload", () => {
  [null, undefined, 42, "nope", [], {}].forEach((value) => {
    const shape = T.shapeTelemetry(value);
    assert.equal(shape.agents.length, 0);
    assert.equal(shape.spans.length, 0);
    assert.equal(shape.totals.events || 0, 0);
  });
  [null, undefined, 42, "nope", []].forEach((value) => {
    assert.ok(T.shapeTelemetry(value).reason.length > 0, "a non-object says why");
  });
  assert.equal(T.shapeTelemetry(null).ok, false);
  assert.equal(T.shapeTelemetry({}).ok, true);
});

test("an empty run says so instead of looking like a zero", () => {
  const shape = T.shapeTelemetry({ run_id: "x", exists: false, agents: [], spans: [], totals: { events: 0 }, reason: "no events are stored for run x" });
  assert.equal(shape.ok, true);
  assert.equal(T.telemetrySummary(shape), "no events are stored for run x");
  assert.equal(T.telemetrySummary(T.shapeTelemetry(null)), "Telemetry unavailable");
});

test("the summary line carries tokens, cost and wall time", () => {
  const line = T.telemetrySummary(T.shapeTelemetry(payload));
  assert.match(line, /2 agents/);
  assert.match(line, /1\.0k in \/ 200 out/);
  assert.match(line, /\$0\.000748/);
  assert.match(line, /4\.0 s/);
  assert.match(line, /1 unpriced call/);
});

test("cost formatting never invents a price", () => {
  assert.equal(T.formatCost(0.000748, "list_price", "USD"), "$0.000748");
  assert.equal(T.formatCost(1.5, "list_price", "USD"), "$1.50");
  assert.equal(T.formatCost(0, "list_price", "USD"), "$0");
  assert.equal(T.formatCost(null, "unpriced", "USD"), "unpriced");
  assert.equal(T.formatCost(null, "partly_unpriced", "USD"), "partly unpriced");
  assert.equal(T.formatCost(null, "no_model_calls", "USD"), "no model calls");
  assert.equal(T.formatCost(null, "", "USD"), "\u2014");
});

test("value formatting picks a unit-appropriate shape", () => {
  assert.equal(T.formatValue(1600, "ms"), "1.6 s");
  assert.equal(T.formatMs(870), "870 ms");
  assert.equal(T.formatMs(null), "\u2014");
  assert.equal(T.formatCount(1200), "1.2k");
  assert.equal(T.formatCount(24000), "24k");
  assert.equal(T.formatBytes(2048), "2.0 kB");
  assert.equal(T.formatValue(null, "ms"), "\u2014");
});

test("the heat scale is a level in 0..1, clamped", () => {
  assert.equal(T.heatLevel(1000, 0, 1000), 1);
  assert.equal(T.heatLevel(500, 0, 1000), 0.5);
  assert.equal(T.heatLevel(2000, 0, 1000), 1, "above the max clamps");
  assert.equal(T.heatLevel(-5, 0, 1000), 0, "below the min clamps");
  assert.equal(T.heatLevel(null, 0, 1000), null, "no value has no colour");
  assert.equal(T.heatLevel(5, 5, 5), 0.5, "a flat column is mid-scale");
  assert.equal(T.heatLevel(0, 0, 0), 0);
});

test("the heat colour is translucent, warmer as the level rises", () => {
  const cold = T.heatColour(0);
  const warm = T.heatColour(0.5);
  const hot = T.heatColour(1);

  assert.equal(T.heatColour(null), "rgba(0, 0, 0, 0)");
  [cold, warm, hot].forEach((colour) => assert.match(colour, /^rgba\(\d+, \d+, \d+, 0\.\d\d\)$/));
  const alpha = (colour) => Number(colour.match(/, ([0-9.]+)\)$/)[1]);
  assert.ok(alpha(cold) < alpha(warm));
  assert.ok(alpha(warm) < alpha(hot));
  assert.ok(hot.startsWith("rgba(224, 113, 111"), "the top of the ramp is the alert red");
});

test("metric grid keeps a null cell null and a zero cell zero", () => {
  const grid = T.metricGrid(heat);

  assert.equal(grid.ok, true);
  assert.equal(grid.empty, false);
  assert.deepEqual(grid.columns.map((column) => column.name), ["tokens_in", "cost_usd"]);
  assert.equal(grid.columns[0].max, 1000);

  const planner = grid.rows[0];
  assert.equal(planner.cells.tokens_in.display, "1.0k");
  assert.equal(planner.cells.tokens_in.level, 1);
  assert.deepEqual(planner.cells.tokens_in.eventIds, ["e1"]);
  assert.equal(planner.cells.tokens_in.key, "planner::tokens_in");

  const writer = grid.rows[1];
  assert.equal(writer.cells.tokens_in.display, "\u2014");
  assert.equal(writer.cells.tokens_in.hasValue, false);
  assert.equal(writer.cells.cost_usd.level, null);
});

test("an unpriced cell with recorded calls stays drillable", () => {
  const unpriced = {
    agents: ["planner"],
    metrics: [{ name: "cost_usd", label: "cost (USD)", unit: "usd" }],
    metric_matrix: {
      planner: { cost_usd: { raw: null, normalised: null, unit: "usd", event_ids: ["e1", "e2"] } },
    },
    metric_normalisation: { basis: "min-max", ranges: {} },
  };
  const cell = T.metricGrid(unpriced).rows[0].cells.cost_usd;

  assert.equal(cell.hasValue, false, "no price is still no price");
  assert.equal(cell.display, "\u2014");
  assert.equal(cell.drillable, true, "the calls behind it are the reason it is null");
  assert.deepEqual(cell.eventIds, ["e1", "e2"]);

  const absent = T.metricGrid(heat).rows[1].cells.cost_usd;
  assert.equal(absent.drillable, false, "nothing recorded means nothing to open");
});

test("metric grid reports emptiness with a reason", () => {
  const empty = T.metricGrid({ agents: [], metrics: [], reason: "no events are stored for run x" });
  assert.equal(empty.empty, true);
  assert.match(empty.reason, /no events/);
  assert.equal(T.metricGrid(null).empty, true);
});

test("bucket grid is stage-ordered and labels its columns", () => {
  const grid = T.bucketGrid(heat);

  assert.deepEqual(grid.columns.map((column) => column.label), ["1. planner", "2. writer"]);
  assert.equal(grid.columns[0].durationMs, 1600);
  assert.equal(grid.rows[1].cells["1"].display, "600 ms");
  assert.equal(grid.rows[1].cells["1"].level, 1);
  assert.equal(grid.rows[0].cells["1"].display, "0 ms");
  assert.deepEqual(grid.rows[1].cells["1"].eventIds, ["s3"]);
  assert.equal(grid.pngFallback, "/api/runs/run-1/heatmap.png");
});

test("waterfall geometry lays spans out per agent row", () => {
  const geometry = T.waterfallGeometry(payload.spans, 400);

  assert.equal(geometry.width, 400);
  assert.equal(geometry.totalMs, 2500, "the open span's start is the furthest point");
  assert.deepEqual(geometry.rows.map((row) => row.agent), ["planner", "writer"]);
  assert.equal(geometry.bars.length, 4);

  const stage = geometry.bars[0];
  assert.equal(stage.agent, "planner");
  assert.equal(stage.x, 0);
  assert.equal(stage.width, Math.round(1600 * (400 / 2500)));
  assert.equal(stage.row, 0);
  assert.equal(stage.offsetLabel, "0 ms");
  assert.equal(stage.durationLabel, "1.6 s");

  const writerBar = geometry.bars.find((bar) => bar.agent === "writer" && bar.kind === "a2a_call");
  assert.equal(writerBar.row, 1);
  assert.ok(writerBar.x > stage.x);

  const open = geometry.bars.find((bar) => bar.open);
  assert.equal(open.durationLabel, "open");
  assert.ok(open.width >= 2, "an open or tiny span is still visible");

  assert.equal(geometry.ticks.length, 5);
  assert.equal(geometry.ticks[4].x, 400);
});

test("waterfall geometry survives nothing to draw", () => {
  const geometry = T.waterfallGeometry([], 400);
  assert.equal(geometry.bars.length, 0);
  assert.equal(geometry.totalMs, 1);
  assert.equal(geometry.height, 22);
  assert.equal(T.waterfallGeometry(null).rows.length, 0);
});

test("the cell query and url match the endpoint contract", () => {
  assert.equal(T.cellsQuery({ agent: "planner", metric: "tokens_in" }), "agent=planner&metric=tokens_in");
  assert.equal(T.cellsQuery({ agent: "planner", bucket: 2 }), "agent=planner&bucket=2");
  assert.equal(
    T.cellsUrl("run/1", { agent: "web researcher", metric: "tool_calls" }),
    "/api/runs/run%2F1/cells?agent=web%20researcher&metric=tool_calls",
  );
  assert.equal(T.cellsUrl("", { agent: "planner", metric: "errors" }), "");
});

test("cell drill-down rows keep the real event identity", () => {
  const rows = T.eventRows({
    events: [
      { event_id: "e1", seq: 3, ts: "2026-01-01T10:00:01+00:00", kind: "model_call", agent: "planner", duration_ms: 1200, tokens_in: 1000, tokens_out: 200, payload_summary: "p" },
      { event_id: "e2", seq: 4, kind: "error", agent: "writer", ok: false, error_message: "no visible text" },
      { kind: "evidence_item", id: "ev-1", source_ref: "https://x/y", retrieved_by: "web_researcher", snippet: "water ice" },
    ],
  });

  assert.equal(rows.length, 3);
  assert.equal(rows[0].key, "e1");
  assert.equal(rows[0].ts, "10:00:01");
  assert.match(rows[0].detail, /1.2 s/);
  assert.match(rows[0].detail, /1.0k in \/ 200 out/);
  assert.equal(rows[1].ok, false);
  assert.match(rows[1].detail, /no visible text/);
  assert.equal(rows[2].kind, "evidence");
  assert.equal(rows[2].text, "https://x/y");
  assert.deepEqual(T.eventRows(null), []);
});

test("the drill-down title names the cell", () => {
  assert.equal(T.cellListTitle({ agent: "planner", metric: "tokens_in" }), "planner \u00b7 tokens in");
  assert.equal(T.cellListTitle({ agent: "planner", bucket: 0 }), "planner \u00b7 stage 1");
  assert.equal(T.cellListTitle(null), "");
});

test("provenance hops read citation to run in order", () => {
  const hops = T.provenanceHops({
    chain: [
      { hop: "citation", source_ref: "https://x/y", quote: "water ice" },
      { hop: "evidence", evidence_id: "ev-1", retrieved_by: "web_researcher", match: "exact" },
      { hop: "source", url: "https://x/y" },
      { hop: "tool_call", tool: "fetch_page", agent: "web_researcher", ok: true, match: "exact" },
      { hop: "agent", agent: "web_researcher" },
      { hop: "run", run_id: "run-1" },
    ],
  });

  assert.deepEqual(hops.map((hop) => hop.kind),
    ["citation", "evidence", "source", "tool_call", "agent", "run"]);
  assert.match(hops[1].detail, /ev-1 retrieved by web_researcher/);
  assert.match(hops[3].detail, /fetch_page by web_researcher/);
  assert.equal(hops[5].detail, "run-1");
  assert.deepEqual(T.provenanceHops({}), []);
});

test("provenance states why there is nothing to show", () => {
  assert.equal(
    T.provenanceUnavailable({ reason: "no stored evidence item contains this claim" }),
    "no stored evidence item contains this claim",
  );
  assert.equal(T.provenanceUnavailable({ missing: ["first reason"] }), "first reason");
  assert.equal(T.provenanceUnavailable({}), "");
  assert.equal(T.provenanceUnavailable(null), "provenance unavailable");
});

test("this module never touches the DOM", () => {
  const source = require("node:fs").readFileSync(require("node:path").join(__dirname, "telemetry.js"), "utf8");
  ["document.", "window.", "innerHTML", "getElementById"].forEach((token) => {
    assert.equal(source.includes(token), false, "telemetry.js must stay DOM-free: " + token);
  });
});
