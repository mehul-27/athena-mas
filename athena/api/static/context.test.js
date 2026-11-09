/* Node harness for the contextual workspace controller. */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const logic = require("./logic.js");

function makeNode(id = "", focusState = null) {
  const classes = new Set();
  const listeners = new Map();
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
    inert: false,
    addEventListener(type, handler) {
      const handlers = listeners.get(type) || [];
      handlers.push(handler);
      listeners.set(type, handlers);
    },
    dispatch(type, event = {}) {
      const actual = {
        target: this,
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
        ...event,
      };
      (listeners.get(type) || []).forEach((handler) => handler(actual));
      return actual;
    },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...children) { this.children = children; },
    focus() { if (focusState) focusState.activeElement = this; },
    closest() { return null; },
    contains(other) { return this === other || (this.focusables || []).includes(other); },
    querySelectorAll() { return this.focusables || []; },
    removeAttribute(name) { delete this.attributes[name]; },
  };
  return node;
}

function textOf(node) {
  if (!node) return "";
  return [node.textContent || "", ...node.children.map(textOf)].join(" ").replace(/\s+/g, " ").trim();
}

function makeHarness() {
  const focusState = { activeElement: null };
  const ids = {};
  ["context-panel", "context-backdrop", "context-close", "context-tabs",
   "context-title", "context-subtitle", "context-status",
   "context-pane-sources", "context-pane-run", "context-pane-context",
   "context-tab-sources", "context-tab-run", "context-tab-context"].forEach((id) => {
    ids[id] = makeNode(id, focusState);
  });
  const shell = makeNode("shell", focusState);
  const appbar = makeNode("appbar", focusState);
  const sidebar = makeNode("sidebar", focusState);
  const stage = makeNode("stage", focusState);
  const idsPanel = ids["context-panel"];
  const backdrop = ids["context-backdrop"];
  const outsideFocus = makeNode("outside-focus", focusState);
  const insideAction = makeNode("inside-action", focusState);
  ids["context-close"].tabIndex = 0;
  insideAction.tabIndex = 0;
  idsPanel.focusables = [ids["context-close"], insideAction];
  idsPanel.contains = (other) => other === idsPanel || idsPanel.focusables.includes(other);
  idsPanel.querySelectorAll = () => idsPanel.focusables;
  const documentListeners = new Map();
  const body = makeNode("body", focusState);
  body.children = [appbar, shell];
  shell.children = [sidebar, stage, idsPanel, backdrop];
  const tabs = ["sources", "run", "context"].map((tab) => {
    const node = ids["context-tab-" + tab];
    node.dataset.contextTab = tab;
    node.tabIndex = -1;
    return node;
  });
  // querySelectorAll('button:not([disabled])') includes these controls even
  // when roving-tabindex has removed them from the natural Tab sequence. The
  // production focus trap must filter to effective tabIndex >= 0.
  idsPanel.focusables = [ids["context-close"], ...tabs, insideAction];
  const document = {
    readyState: "complete",
    body,
    get activeElement() { return focusState.activeElement; },
    getElementById: (id) => ids[id] || null,
    createElement: () => makeNode("", focusState),
    querySelector: (selector) => selector === ".shell" ? shell : null,
    querySelectorAll: (selector) => selector === "#context-tabs .context-tab" ? tabs : [],
    addEventListener(type, handler) {
      const handlers = documentListeners.get(type) || [];
      handlers.push(handler);
      documentListeners.set(type, handlers);
    },
    dispatch(type, event = {}) {
      const actual = {
        target: focusState.activeElement,
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
        ...event,
      };
      (documentListeners.get(type) || []).forEach((handler) => handler(actual));
      return actual;
    },
  };
  const storage = new Map();
  const localStorage = {
    getItem: (key) => storage.has(key) ? storage.get(key) : null,
    setItem: (key, value) => storage.set(key, String(value)),
  };
  const context = {
    window: {
      innerWidth: 1200,
      addEventListener(type, handler) {
        const handlers = windowListeners.get(type) || [];
        handlers.push(handler);
        windowListeners.set(type, handlers);
      },
      dispatch(type) { (windowListeners.get(type) || []).forEach((handler) => handler()); },
    },
    document,
    localStorage,
    requestAnimationFrame: (fn) => { fn(); return 1; },
    setTimeout,
    console,
    AthenaLogic: logic,
    AthenaUi: {
      activityRow(row) {
        const node = makeNode("", focusState);
        node.textContent = [row.label, row.status].filter(Boolean).join(" ");
        return node;
      },
      showView() {},
    },
  };
  const windowListeners = new Map();
  context.globalThis = context;
  vm.createContext(context);
  const source = fs.readFileSync(path.join(__dirname, "context.js"), "utf8");
  vm.runInContext(source, context);
  return { context, ids, tabs, body, document, focusState, outsideFocus, insideAction, appbar, sidebar, stage };
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

test("desktop context rail stays nonmodal and does not inert the shell", () => {
  const { context, ids, body, appbar, sidebar, stage } = makeHarness();
  context.window.innerWidth = 1440;
  context.window.dispatch("resize");
  assert.equal(body.classList.contains("context-rail"), true);
  assert.equal(ids["context-panel"].inert, true);
  assert.equal(ids["context-panel"].getAttribute("aria-hidden"), "true");
  context.AthenaContext.open("sources", "research", "r1");

  assert.equal(body.classList.contains("context-rail"), true);
  assert.equal(body.classList.contains("context-overlay"), false);
  assert.equal(ids["context-panel"].getAttribute("role"), "complementary");
  assert.equal(ids["context-panel"].getAttribute("aria-modal"), null);
  assert.equal(ids["context-backdrop"].classList.contains("hidden"), true);
  assert.equal(appbar.inert, false);
  assert.equal(sidebar.inert, false);
  assert.equal(stage.inert, false);
});

test("mobile modal traps Tab, handles document Escape, and restores opener and inert state", () => {
  const { context, ids, body, document, focusState, outsideFocus, insideAction, appbar, sidebar, stage } = makeHarness();
  const opener = makeNode("opener", focusState);
  stage.inert = true; // A pre-existing inert value must survive the modal.
  context.AthenaContext.open("sources", "research", "r1", opener);

  assert.equal(ids["context-panel"].getAttribute("role"), "dialog");
  assert.equal(ids["context-panel"].getAttribute("aria-modal"), "true");
  assert.equal(body.classList.contains("context-overlay"), true);
  assert.equal(ids["context-backdrop"].classList.contains("hidden"), false);
  assert.equal([appbar.inert, sidebar.inert, stage.inert].every(Boolean), true);

  const close = ids["context-close"];
  assert.equal(document.activeElement, close);
  focusState.activeElement = insideAction;
  let event = document.dispatch("keydown", { key: "Tab", shiftKey: false });
  assert.equal(event.defaultPrevented, true);
  assert.equal(document.activeElement, close);
  focusState.activeElement = close;
  event = document.dispatch("keydown", { key: "Tab", shiftKey: true });
  assert.equal(event.defaultPrevented, true);
  assert.equal(document.activeElement, insideAction);

  focusState.activeElement = outsideFocus;
  event = document.dispatch("keydown", { key: "Tab", shiftKey: false });
  assert.equal(event.defaultPrevented, true);
  assert.equal(document.activeElement, close);

  focusState.activeElement = outsideFocus;
  event = document.dispatch("keydown", { key: "Escape" });
  assert.equal(event.defaultPrevented, true);
  assert.equal(ids["context-panel"].classList.contains("hidden"), true);
  assert.equal(ids["context-backdrop"].classList.contains("hidden"), true);
  assert.equal(body.classList.contains("context-overlay"), false);
  assert.equal(document.activeElement, opener);
  assert.equal(appbar.inert, false);
  assert.equal(sidebar.inert, false);
  assert.equal(stage.inert, true);
});

test("backdrop click restores focus, and resize moves focus into then out of modal mode", () => {
  const { context, ids, body, document, focusState, outsideFocus, insideAction, appbar, sidebar, stage } = makeHarness();
  context.window.innerWidth = 1440;
  const opener = makeNode("opener", focusState);
  context.AthenaContext.open("sources", "research", "r1", opener);

  // Moving into the modal breakpoint while focus is outside must put focus in
  // the panel before background content becomes unusable.
  focusState.activeElement = outsideFocus;
  context.window.innerWidth = 1200;
  context.window.dispatch("resize");
  assert.equal(document.activeElement, ids["context-close"]);
  assert.equal([appbar.inert, sidebar.inert, stage.inert].every(Boolean), true);

  // Existing focus inside the panel survives a resize back to desktop, and
  // inert state is restored without closing the selected context.
  focusState.activeElement = insideAction;
  context.window.innerWidth = 1440;
  context.window.dispatch("resize");
  assert.equal(document.activeElement, insideAction);
  assert.equal(body.classList.contains("context-rail"), true);
  assert.equal(body.classList.contains("context-overlay"), false);
  assert.equal(appbar.inert, false);
  assert.equal(sidebar.inert, false);
  assert.equal(stage.inert, false);

  context.window.innerWidth = 1200;
  context.window.dispatch("resize");
  ids["context-backdrop"].dispatch("click");
  assert.equal(ids["context-panel"].classList.contains("hidden"), true);
  assert.equal(document.activeElement, opener);
  assert.equal([appbar.inert, sidebar.inert, stage.inert].every((value) => !value), true);
});
