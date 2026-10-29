/* Node harness for composer submission behavior. No DOM library or network. */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const logic = require("./logic.js");

const APP = path.join(__dirname, "app.js");
const MARKER = "/* ---------------------------------------------------------------- wiring */";

function makeHarness() {
  const nodes = new Map();
  function node(value = "") {
    const item = {
      value,
      textContent: "",
      className: "",
      children: [],
      style: {},
      disabled: false,
      classList: {
        _set: new Set(),
        add(...names) { names.forEach((name) => this._set.add(name)); },
        remove(...names) { names.forEach((name) => this._set.delete(name)); },
        toggle(name, force) {
          const on = force === undefined ? !this._set.has(name) : Boolean(force);
          if (on) this._set.add(name); else this._set.delete(name);
        },
        contains(name) { return this._set.has(name); },
      },
      setAttribute() {},
      removeAttribute() {},
      addEventListener() {},
      focus() {},
      appendChild(child) { this.children.push(child); return child; },
      replaceChildren(...children) { this.children = children; },
    };
    return item;
  }
  function getNode(id) {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  }

  const document = {
    getElementById: getNode,
    createElement: () => node(),
    querySelectorAll: () => [],
    querySelector: () => null,
  };
  const source = fs.readFileSync(APP, "utf8");
  const end = source.indexOf(MARKER);
  assert.notEqual(end, -1, "app.js wiring marker must exist");
  const script = source.slice(0, end) + `
    globalThis.__composer = {
      startRun,
      state,
      renderEvidenceTable,
      loadMetrics,
      resetMetrics,
      setApi(fn) { api = fn; },
      install(overrides) {
        api = overrides.api;
        setBusy = overrides.setBusy;
        showView = overrides.showView;
        showExchange = overrides.showExchange;
        beginRun = overrides.beginRun;
        resetReport = overrides.resetReport;
        addLog = overrides.addLog;
        markStartFailed = overrides.markStartFailed;
        refreshHealth = overrides.refreshHealth;
        toast = overrides.toast;
        autoGrow = overrides.autoGrow;
      },
    };
  `;
  const context = {
    window: { AthenaLogic: logic, AthenaDiagram: {} },
    document,
    console,
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(script, context);
  return { app: context.__composer, document };
}

function install(app, document, reply) {
  const calls = { bodies: [], shown: [], begun: [], toasts: [] };
  app.install({
    api: async (url, options) => {
      calls.url = url;
      calls.bodies.push(JSON.parse(options.body));
      return reply();
    },
    setBusy: () => {},
    showView: () => {},
    showExchange: (question) => calls.shown.push(question),
    beginRun: (runId) => {
      calls.begun.push(runId);
      calls.questionAtBegin = document.getElementById("question").value;
    },
    resetReport: () => {},
    addLog: () => {},
    markStartFailed: () => {},
    refreshHealth: () => {},
    toast: (message) => calls.toasts.push(message),
    autoGrow: () => {},
  });
  return calls;
}

function prepare(question, pattern = "hierarchical") {
  const harness = makeHarness();
  harness.document.getElementById("question").value = question;
  harness.document.getElementById("composer-pattern").value = pattern;
  return harness;
}

test("successful submission sends the original question and clears after a valid run id", async () => {
  const question = "  What changed in Python 3.13?  ";
  const { app, document } = prepare(question, "p2p");
  const calls = install(app, document, () => ({ run_id: "run-123" }));

  await app.startRun();

  assert.deepEqual(calls.bodies, [{ question, pattern: "p2p" }]);
  assert.deepEqual(calls.shown, [question]);
  assert.deepEqual(calls.begun, ["run-123"]);
  assert.equal(calls.questionAtBegin, "", "the valid run id unlocks the clear");
  assert.equal(document.getElementById("question").value, "");
  assert.equal(document.getElementById("composer-pattern").value, "p2p");
  assert.equal(app.state.pattern, "p2p");
});

test("a failed request preserves the question and pattern", async () => {
  const question = "keep this draft";
  const { app, document } = prepare(question, "parallel");
  const calls = install(app, document, () => {
    throw new Error("provider unavailable");
  });

  await app.startRun();

  assert.equal(document.getElementById("question").value, question);
  assert.equal(calls.begun.length, 0);
  assert.equal(app.state.pattern, "parallel");
  assert.ok(calls.toasts.length > 0);
});

test("HTTP 409 preserves the question", async () => {
  const question = "retry me";
  const { app, document } = prepare(question, "hierarchical");
  const calls = install(app, document, () => {
    const error = new Error("run is still going");
    error.status = 409;
    throw error;
  });

  await app.startRun();

  assert.equal(document.getElementById("question").value, question);
  assert.equal(calls.begun.length, 0);
  assert.equal(app.state.pattern, "hierarchical");
});

test("missing or invalid run ids preserve the question", async () => {
  for (const reply of [() => ({}), () => ({ run_id: "" }), () => ({ run_id: 7 })]) {
    const question = "wait for a real id";
    const { app, document } = prepare(question, "sequential");
    const calls = install(app, document, reply);

    await app.startRun();

    assert.equal(document.getElementById("question").value, question);
    assert.equal(calls.begun.length, 0);
    assert.equal(app.state.pattern, "sequential");
  }
});

test("blackboard evidence renderer keeps the full text behind a preview", () => {
  const { app, document } = makeHarness();
  const box = document.createElement("div");
  const full = "long source text " + "x".repeat(5000);

  app.renderEvidenceTable(box, [{
    retrieved_by: "researcher",
    source_ref: "https://example.com/" + "a".repeat(300),
    score: 0.5,
    text: full,
  }]);

  const walk = (node) => {
    const found = [node];
    for (const child of node.children) found.push(...walk(child));
    return found;
  };
  const all = walk(box);
  const pre = all.find((item) => item.className === "evidence-full");
  const wrap = all.find((item) => item.className === "table-wrap");

  assert.ok(wrap, "the evidence table must be inside a scroll container");
  assert.ok(pre, "full evidence must be available");
  assert.equal(pre.textContent, full, "the full content must not be truncated");
});

test("metrics renderer handles populated, empty, and failed responses", async () => {
  const { app, document } = makeHarness();
  const table = document.getElementById("agent-metrics-table");
  const body = document.getElementById("agent-metrics-body");
  const empty = document.getElementById("metrics-empty");
  const heatmap = document.getElementById("heatmap");

  app.setApi(async () => ({ rows: [{ agent: "researcher", stage_ms: 1200, tokens_in: 9 }] }));
  await app.loadMetrics("r1");
  assert.equal(body.children.length, 1);
  assert.equal(table.classList.contains("hidden"), false);
  assert.equal(empty.classList.contains("hidden"), true);
  assert.ok(heatmap.src.includes("/api/runs/r1/heatmap.png"));

  app.setApi(async () => ({ rows: [] }));
  await app.loadMetrics("r1");
  assert.equal(table.classList.contains("hidden"), true);
  assert.equal(empty.classList.contains("hidden"), false);
  assert.match(empty.textContent, /No per-agent metrics were recorded/);

  app.setApi(async () => { throw new Error("offline"); });
  await app.loadMetrics("r1");
  assert.match(empty.textContent, /Could not load per-agent metrics/);
});
