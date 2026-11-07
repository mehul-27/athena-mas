/* Node tests for the evaluation dashboard's measurement rules.
   Run: node athena/api/static/telemetry.test.js is unrelated; this file covers
   logic.evalPercent / evalQualityLabel / evalOperationalRows. */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const logic = require("./logic.js");

const OPERATIONAL = {
  attempts: 33,
  terminal_states: { failed: 33 },
  failure_codes: { provider_quota: 32, local_rate_limit: 1 },
  model_start: { hits: 6, n: 33, rate: 0.1818 },
  research_completion: { hits: 0, n: 33, rate: 0 },
  critic_approval: { hits: 0, n: 0, rate: null },
  critic_evaluable_runs: 0,
  scoring_success: { hits: 0, n: 33, rate: 0 },
  infrastructure_failure: { hits: 33, n: 33, rate: 1 },
  infrastructure_blocked: { hits: 27, n: 33, rate: 0.8182 },
  interrupted_after_model: { hits: 6, n: 33, rate: 0.1818 },
  valid_completed_runs: 0,
  research_quality: {
    mean: null,
    n: 0,
    measurable: false,
    display: "N/A - no valid completed research runs",
  },
};

test("a measured rate renders as a percentage with its counts", () => {
  assert.equal(logic.evalPercent({ hits: 6, n: 33, rate: 0.1818 }), "18.2% (6/33)");
  assert.equal(logic.evalPercent({ hits: 27, n: 33, rate: 0.8182 }), "81.8% (27/33)");
});

test("a genuine zero rate is shown as 0%, not as missing", () => {
  assert.equal(logic.evalPercent({ hits: 0, n: 33, rate: 0 }), "0% (0/33)");
});

test("an empty denominator is 'not measurable', never 0%", () => {
  const text = logic.evalPercent({ hits: 0, n: 0, rate: null });
  assert.equal(text, "not measurable");
  assert.notEqual(text, "0% (0/0)");
});

test("a missing metric object renders as a dash rather than a zero", () => {
  assert.equal(logic.evalPercent(null), "\u2014");
  assert.equal(logic.evalPercent(undefined), "\u2014");
});

test("quality is undefined, not zero, when nothing completed", () => {
  const text = logic.evalQualityLabel(OPERATIONAL);
  assert.equal(text, "N/A - no valid completed research runs");
  assert.ok(!/^0(\.0+)?$/.test(text), "an unmeasured quality must not look like a score");
});

test("quality shows the mean and n when it was measured", () => {
  const measured = {
    research_quality: { mean: 1, n: 1, measurable: true },
  };
  assert.equal(logic.evalQualityLabel(measured), "1.000 (n=1)");
});

test("a measured zero quality is displayed as zero, not N/A", () => {
  const measured = { research_quality: { mean: 0, n: 2, measurable: true } };
  const text = logic.evalQualityLabel(measured);
  assert.equal(text, "0.000 (n=2)");
  assert.ok(!text.includes("N/A"));
});

test("the operational rows cover every requested metric", () => {
  const rows = logic.evalOperationalRows(OPERATIONAL);
  const labels = rows.map((pair) => pair[0]);
  const text = rows.map((pair) => pair[1]).join(" | ");

  for (const label of [
    "Attempts",
    "Terminal states",
    "Failure causes",
    "Model-start rate",
    "Research completion",
    "Critic approval",
    "Scoring success",
    "Infrastructure failures",
    "Research quality",
  ]) {
    assert.ok(labels.includes(label), "missing row: " + label);
  }
  // The exact values from the investigation.
  assert.ok(text.includes("18.2% (6/33)"), "model-start rate");
  assert.ok(text.includes("81.8% (27/33)"), "infrastructure blocked");
  assert.ok(text.includes("not measurable"), "critic approval denominator");
  assert.ok(text.includes("N/A - no valid completed research runs"), "quality");
  assert.ok(text.includes("provider_quota=32"), "failure causes");
});

test("the rows are not produced without an operational block", () => {
  assert.deepEqual(logic.evalOperationalRows(null), []);
  assert.deepEqual(logic.evalOperationalRows({}), []);
  assert.deepEqual(logic.evalOperationalRows({ attempts: 0 }), []);
});

test("a batch with a measured quality reports the mean and sample size", () => {
  const operational = {
    attempts: 36,
    terminal_states: { completed: 1, failed: 35 },
    failure_codes: { provider_quota: 32 },
    model_start: { hits: 7, n: 36, rate: 0.1944 },
    research_completion: { hits: 1, n: 36, rate: 0.0278 },
    critic_approval: { hits: 1, n: 1, rate: 1 },
    critic_evaluable_runs: 1,
    scoring_success: { hits: 1, n: 36, rate: 0.0278 },
    infrastructure_failure: { hits: 35, n: 36, rate: 0.9722 },
    infrastructure_blocked: { hits: 29, n: 36, rate: 0.8056 },
    interrupted_after_model: { hits: 6, n: 36, rate: 0.1667 },
    valid_completed_runs: 1,
    research_quality: { mean: 1, n: 1, measurable: true },
  };
  const text = logic
    .evalOperationalRows(operational)
    .map((pair) => pair[1])
    .join(" | ");

  assert.ok(text.includes("100% (1/1)"), "critic approval with a real denominator");
  assert.ok(text.includes("1.000 (n=1)"), "measured quality");
  assert.ok(!text.includes("N/A"), "quality was measurable here");
});
