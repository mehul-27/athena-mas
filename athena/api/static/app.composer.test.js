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
      dataset: {},
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
      openRun,
      loadRunEvents,
      state,
      renderEvidenceTable,
      loadMetrics,
      resetMetrics,
      setApi(fn) { api = fn; },
      installEventRender(overrides) {
        renderTimeline = overrides.renderTimeline || (() => {});
        renderLiveMap = overrides.renderLiveMap || (() => {});
        updateProgress = overrides.updateProgress || (() => {});
        renderActivityTrace = overrides.renderActivityTrace || (() => {});
        logEvent = overrides.logEvent || (() => {});
      },
      installOpenRun(overrides) {
        api = overrides.api;
        showView = overrides.showView;
        setPattern = overrides.setPattern;
        beginRun = overrides.beginRun;
        showExchange = overrides.showExchange;
        resetReport = overrides.resetReport;
        renderDetailsHeader = overrides.renderDetailsHeader;
        renderBlackboard = overrides.renderBlackboard;
        renderToolReport = overrides.renderToolReport;
        loadRunEvents = overrides.loadRunEvents;
        renderReport = overrides.renderReport;
        loadMetrics = overrides.loadMetrics;
        renderLiveMap = overrides.renderLiveMap;
        renderStatus = overrides.renderStatus;
        setRunning = overrides.setRunning;
        toast = overrides.toast;
      },
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
    history: { replaceState() {} },
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

  assert.deepEqual(calls.bodies, [{ question, pattern: "p2p", tools: null }]);
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

test("a custom tool selection is sent with the run", async () => {
  const { app, document } = prepare("Which release changed this?");
  app.state.researchTools = ["web_search", "fetch_page"];
  const calls = install(app, document, () => ({ run_id: "run-tools" }));

  await app.startRun();

  assert.deepEqual(calls.bodies, [{
    question: "Which release changed this?",
    pattern: "hierarchical",
    tools: ["web_search", "fetch_page"],
  }]);
});

test("no tools blocks the run and explains the state", async () => {
  const { app, document } = prepare("Do not start this");
  app.state.researchTools = [];
  let requested = false;
  const calls = install(app, document, () => {
    requested = true;
    return { run_id: "should-not-start" };
  });

  await app.startRun();

  assert.equal(requested, false);
  assert.deepEqual(calls.bodies, []);
  assert.ok(calls.toasts.some((message) => /at least one research tool/i.test(message)));
});

function installOpenRun(app) {
  const calls = { begun: [], shown: [], toasts: [] };
  app.installOpenRun({
    api: () => Promise.reject(new Error("not configured")),
    showView: () => {},
    setPattern: () => {},
    beginRun: (runId) => {
      app.state.runId = runId;
      app.state.loadingRunId = null;
      calls.begun.push(runId);
    },
    showExchange: (question) => calls.shown.push(question),
    resetReport: () => {},
    renderDetailsHeader: () => {},
    renderBlackboard: () => {},
    renderToolReport: () => {},
    loadRunEvents: async () => {},
    renderReport: () => {},
    loadMetrics: async () => {},
    renderLiveMap: () => {},
    renderStatus: () => {},
    setRunning: () => {},
    toast: (message) => calls.toasts.push(message),
  });
  return calls;
}

test("rapid history switches keep the last selected run", async () => {
  const { app } = makeHarness();
  const calls = installOpenRun(app);
  const pending = {};
  app.installOpenRun({
    ...{
      api: (url) => new Promise((resolve) => { pending[url] = resolve; }),
      showView: () => {},
      setPattern: () => {},
      beginRun: (runId) => {
        app.state.runId = runId;
        app.state.loadingRunId = null;
        calls.begun.push(runId);
      },
      showExchange: () => {},
      resetReport: () => {},
      renderDetailsHeader: () => {},
      renderBlackboard: () => {},
      renderToolReport: () => {},
      loadRunEvents: async () => {},
      renderReport: () => {},
      loadMetrics: async () => {},
      renderLiveMap: () => {},
      renderStatus: () => {},
      setRunning: () => {},
      toast: () => {},
    },
  });

  const first = app.openRun("run-a");
  const second = app.openRun("run-b");
  assert.equal(app.state.loadingRunId, "run-b");
  pending["/api/runs/run-b"]({ task: { value: { question: "B" } } });
  await second;
  pending["/api/runs/run-a"]({ task: { value: { question: "A" } } });
  await first;

  assert.equal(app.state.runId, "run-b");
  assert.equal(app.state.loadingRunId, null);
  assert.deepEqual(calls.begun, ["run-b"]);
});

test("a failed event load is explicit instead of looking empty", async () => {
  const { app } = makeHarness();
  app.installEventRender({});
  app.state.runId = "run-a";
  app.state.events = [{ event_id: "old" }];
  app.setApi(async () => { throw new Error("offline"); });

  await app.loadRunEvents("run-a");

  assert.deepEqual(app.state.events, []);
  assert.equal(app.state.eventsError, "Activity could not be loaded for this run.");
});

test("stale event loads cannot overwrite the selected run", async () => {
  const { app } = makeHarness();
  const previous = [{ event_id: "keep" }];
  app.state.runId = "run-a";
  app.state.events = previous;
  let resolve;
  app.setApi(() => new Promise((done) => { resolve = done; }));

  const pending = app.loadRunEvents("run-a");
  app.state.runId = "run-b";
  resolve({ events: [{ event_id: "stale" }] });
  await pending;

  assert.deepEqual(app.state.events, previous);
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
