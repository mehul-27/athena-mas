/* Node harness for the contextual workspace controller. */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const logic = require("./logic.js");

function makeNode(id = "") {
  const classes = new Set();
  const node = {
    id,
    textContent: "",
    className: "",
    children: [],
    dataset: {},
    attributes: {},
    href: "",
    target: "",
    rel: "",
    title: "",
    type: "",
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      toggle(name, force) {
        const on = force === undefined ? !classes.has(name) : Boolean(force);
        if (on) classes.add(name); else classes.delete(name);
      },
      contains(name) { return classes.has(name); },
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] == null ? null : this.attributes[name]; },
    addEventListener() {},
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...children) { this.children = children; },
    focus() {},
    closest() { return null; },
  };
  return node;
}

function textOf(node) {
  if (!node) return "";
  return [node.textContent || "", ...node.children.map(textOf)].join(" ").replace(/\s+/g, " ").trim();
}

function makeHarness() {
  const ids = {};
  ["context-panel", "context-backdrop", "context-close", "context-tabs",
   "context-title", "context-subtitle", "context-status",
   "context-pane-sources", "context-pane-run", "context-pane-context",
   "context-tab-sources", "context-tab-run", "context-tab-context"].forEach((id) => {
    ids[id] = makeNode(id);
  });
  const tabs = ["sources", "run", "context"].map((tab) => {
    const node = ids["context-tab-" + tab];
    node.dataset.contextTab = tab;
    return node;
  });
  const body = makeNode("body");
  const document = {
    readyState: "complete",
    body,
    getElementById: (id) => ids[id] || null,
    createElement: () => makeNode(),
    querySelector: () => null,
    querySelectorAll: (selector) => selector === "#context-tabs .context-tab" ? tabs : [],
    addEventListener() {},
  };
  const storage = new Map();
  const localStorage = {
    getItem: (key) => storage.has(key) ? storage.get(key) : null,
    setItem: (key, value) => storage.set(key, String(value)),
  };
  const context = {
    window: { innerWidth: 1200, addEventListener() {} },
    document,
    localStorage,
    requestAnimationFrame: (fn) => { fn(); return 1; },
    setTimeout,
    console,
    AthenaLogic: logic,
    AthenaUi: {
      activityRow(row) {
        const node = makeNode();
        node.textContent = [row.label, row.status].filter(Boolean).join(" ");
        return node;
      },
      showView() {},
    },
  };
  context.globalThis = context;
  vm.createContext(context);
  const source = fs.readFileSync(path.join(__dirname, "context.js"), "utf8");
  vm.runInContext(source, context);
  return { context, ids, tabs, body };
}

function model(kind, id, title, prompt, sourceTitle) {
  return {
    kind,
    id,
    title,
    subtitle: kind === "research" ? "run r1 · sequential" : "openai · test",
    status: { state: "completed", label: "Completed", tone: "ok" },
    rag: { state: "passages", label: "RAG 1 passage", count: 1, detail: "1 passage retrieved." },
    sources: [{
      id: "s1", title: sourceTitle, type: "Document passage",
      location: "handbook.pdf · page 4", url: "", ref: "handbook.pdf#p4",
      page: 4, score: 0.8, excerpt: "A short excerpt.",
      badges: ["Document passage", "Retrieved", "Cited in report"], cited: true,
    }],
    citationCount: 1,
    run: {
      status: { state: "completed", label: "Completed", tone: "ok" },
      rows: [{ agent: "planner", label: "Planner", status: "done" }],
      durationMs: 1200, pattern: kind === "research" ? "sequential" : "",
      tools: { enabled: ["rag_search"], planned: ["rag_search"], plannedBlocked: [], invoked: ["rag_search"], failed: [], skipped: [], selected: [] },
      warnings: [], canOpenDetails: kind === "research", kind,
    },
    context: {
      prompt,
      items: [{ label: "Pattern", value: kind === "research" ? "sequential" : "chat" }],
      notes: ["A limitation."],
    },
  };
}

test("panel opens, selects tabs, and renders the correct surface data", () => {
  const { context, ids, body } = makeHarness();
  context.AthenaContext.register("research", (id) => id === "r1" ? model("research", "r1", "Research run", "What changed?", "handbook.pdf") : null);

  context.AthenaContext.open("sources", "research", "r1");
  assert.equal(ids["context-panel"].classList.contains("hidden"), false);
  assert.equal(body.classList.contains("context-open"), true);
  assert.equal(ids["context-title"].textContent, "Research run");
  assert.match(textOf(ids["context-pane-sources"]), /handbook\.pdf/);
  assert.match(textOf(ids["context-pane-sources"]), /Cited in report/);
  assert.match(textOf(ids["context-pane-run"]), /Planner/);
  assert.match(textOf(ids["context-pane-context"]), /What changed\?/);
  assert.deepEqual(context.AthenaContext.target(), { kind: "research", id: "r1" });

  context.AthenaContext.setTab("run");
  assert.equal(ids["context-pane-sources"].classList.contains("hidden"), true);
  assert.equal(ids["context-pane-run"].classList.contains("hidden"), false);
  assert.equal(ids["context-tab-run"].getAttribute("aria-selected"), "true");
  assert.equal(ids["context-tab-sources"].getAttribute("aria-selected"), "false");
});

test("switching surfaces clears stale content and close keeps the conversation intact", () => {
  const { context, ids, body } = makeHarness();
  context.AthenaContext.register("research", (id) => id === "r1" ? model("research", "r1", "Research run", "Research prompt", "research.pdf") : null);
  context.AthenaContext.register("chat", (id) => id === "m1" ? model("chat", "m1", "Chat response", "Chat prompt", "chat.pdf") : null);

  context.AthenaContext.open("sources", "research", "r1");
  context.AthenaContext.setSurface("chat", "m1");
  assert.match(textOf(ids["context-pane-sources"]), /chat\.pdf/);
  assert.doesNotMatch(textOf(ids["context-pane-sources"]), /research\.pdf/);
  assert.equal(ids["context-title"].textContent, "Chat response");

  context.AthenaContext.close(false);
  assert.equal(ids["context-panel"].classList.contains("hidden"), true);
  assert.equal(body.classList.contains("context-open"), false);
  assert.deepEqual(context.AthenaContext.target(), { kind: "chat", id: "m1" });
});

test("missing selections show an explicit empty state and views hide the panel", () => {
  const { context, ids } = makeHarness();
  context.AthenaContext.register("research", () => null);
  context.AthenaContext.open("context", "research", "missing");
  assert.equal(ids["context-title"].textContent, "No response selected");
  assert.match(textOf(ids["context-pane-sources"]), /No response is selected/);

  context.AthenaContext.setView("details");
  assert.equal(ids["context-panel"].classList.contains("hidden"), true);
  context.AthenaContext.setView("run");
  assert.equal(ids["context-panel"].classList.contains("hidden"), false);
});

test("refresh updates the selected model without changing the target", () => {
  const { context, ids } = makeHarness();
  let sourceTitle = "first.pdf";
  context.AthenaContext.register("research", (id) => id === "r1" ? model("research", "r1", "Research run", "Prompt", sourceTitle) : null);
  context.AthenaContext.open("sources", "research", "r1");
  assert.match(textOf(ids["context-pane-sources"]), /first\.pdf/);

  sourceTitle = "second.pdf";
  context.AthenaContext.refresh("research", "r1");
  assert.match(textOf(ids["context-pane-sources"]), /second\.pdf/);
  assert.doesNotMatch(textOf(ids["context-pane-sources"]), /first\.pdf/);
  assert.deepEqual(context.AthenaContext.target(), { kind: "research", id: "r1" });
});
