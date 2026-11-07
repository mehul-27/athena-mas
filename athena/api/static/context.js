/* One contextual workspace shared by Chat and Research.
 * The data is supplied by the active surface; this module only renders it.
 */

/* The one breakpoint this panel has: at or above it the panel sits in the flow
 * next to the conversation, below it the panel is an overlay with a backdrop.
 * style.css repeats the number in its `--context-breakpoint` media queries;
 * tests/test_ui_static.py fails if the two drift apart. */
const CONTEXT_BREAKPOINT = 1280;

(function (root) {
  "use strict";

  /* Only the active tab is remembered. An `open` flag used to be stored too,
   * which made the panel come back open on every load; the panel now always
   * starts closed and is opened by an explicit Context control. */
  const STORAGE_KEY = "athena.context.panel.v2";
  const TABS = ["sources", "run", "context"];
  const providers = new Map();
  const state = {
    open: false,
    tab: "sources",
    surface: "research",
    target: null,
    view: "run",
    pendingSource: null,
  };
  let lastTrigger = null;
  let lastAction = null;

  const $ = (id) => document.getElementById(id);
  const text = (node, value) => {
    if (node) node.textContent = value == null ? "" : String(value);
  };

  function loadPrefs() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (raw && typeof raw === "object") {
        state.tab = TABS.includes(raw.tab) ? raw.tab : "sources";
      }
    } catch (_) { /* private mode */ }
  }

  function savePrefs() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ tab: state.tab }));
    } catch (_) { /* private mode */ }
  }

  function register(kind, provider) {
    if (typeof provider === "function") providers.set(kind, provider);
  }

  function target() {
    return state.target ? { kind: state.target.kind, id: state.target.id } : null;
  }

  function visible() {
    return state.open && (state.view === "chat" || state.view === "run");
  }

  function isOverlay() {
    return Number(window.innerWidth || 0) < CONTEXT_BREAKPOINT;
  }

  function syncShell() {
    const panel = $("context-panel");
    const backdrop = $("context-backdrop");
    const shown = visible();
    const overlay = shown && isOverlay();
    if (panel) {
      panel.classList.toggle("hidden", !shown);
      panel.classList.toggle("is-overlay", overlay);
      panel.setAttribute("aria-hidden", shown ? "false" : "true");
    }
    if (backdrop) backdrop.classList.toggle("hidden", !overlay);
    document.body.classList.toggle("context-open", shown);
    document.body.classList.toggle("context-overlay", overlay);
    ["context-toggle", "research-context-toggle"].forEach((id) => {
      const toggle = $(id);
      if (toggle) toggle.setAttribute("aria-expanded", shown ? "true" : "false");
    });
  }

  function setView(view) {
    state.view = String(view || "run");
    syncShell();
    if (state.open) render();
  }

  function setSurface(kind, id) {
    state.surface = kind;
    state.target = id ? { kind, id: String(id) } : null;
    syncShell();
    if (state.open) render();
  }

  function open(tab, kind, id, trigger) {
    state.open = true;
    state.surface = kind;
    state.target = id ? { kind, id: String(id) } : null;
    state.tab = TABS.includes(tab) ? tab : "sources";
    lastTrigger = trigger || null;
    lastAction = state.tab;
    savePrefs();
    syncShell();
    render();
    requestAnimationFrame(() => {
      const close = $("context-close");
      if (close) close.focus({ preventScroll: true });
    });
  }

  /* Open at one specific source, used by clickable `[n]` citation markers. */
  function openSource(selection, kind, id, trigger) {
    state.pendingSource = selection == null ? null : String(selection);
    open("sources", kind, id, trigger);
  }

  /* The header toggle: open/close without losing the last title/tab. */
  function toggle(tab, kind, id, trigger) {
    if (visible()) {
      close(true);
      return;
    }
    open(tab || state.tab, kind, id, trigger);
  }

  function focusReturnTarget() {
    if (lastTrigger && lastTrigger.isConnected !== false
        && typeof lastTrigger.focus === "function") {
      lastTrigger.focus({ preventScroll: true });
      return;
    }
    const target = state.target;
    const action = lastAction || "sources";
    let node = null;
    if (target && target.kind === "research") {
      node = document.querySelector(
        '#research-response-meta [data-context-action="' + action + '"]'
      );
    } else if (target && target.kind === "chat") {
      const turns = document.querySelectorAll("[data-message-id]");
      const turn = Array.from(turns).find(
        (item) => item.dataset.messageId === target.id
      );
      node = turn
        ? turn.querySelector('.response-meta-pill[data-context-action="' + action + '"]')
        : null;
    }
    if (node && typeof node.focus === "function") {
      node.focus({ preventScroll: true });
    }
  }

  function close(focusTrigger = true) {
    state.open = false;
    state.pendingSource = null;
    savePrefs();
    syncShell();
    if (focusTrigger) focusReturnTarget();
    lastTrigger = null;
    lastAction = null;
  }

  function setTab(tab, focus = false) {
    if (!TABS.includes(tab)) return;
    state.tab = tab;
    savePrefs();
    if (state.open) render();
    if (focus) {
      const button = $("context-tab-" + tab);
      if (button) button.focus({ preventScroll: true });
    }
  }

  function refresh(kind, id) {
    if (!state.open || !state.target) return;
    if (kind && state.target.kind !== kind) return;
    if (id && state.target.id !== String(id)) return;
    render();
  }

  function el(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) node.textContent = String(value);
    return node;
  }

  function emptyState(message) {
    return el("p", "context-empty", message);
  }

  function statusChip(status) {
    const chip = el("span", "context-status-chip is-" + (status.tone || "muted"));
    text(chip, status.label || "");
    return chip;
  }

  function sourceSummary(model) {
    const sources = model.sources || [];
    const fetched = sources.filter((source) => (source.badges || []).includes("Page fetched")).length;
    const cited = sources.filter((source) => source.cited).length;
    const parts = [sources.length + " retrieved"];
    if (fetched) parts.push(fetched + " fetched");
    if (model.kind === "research") {
      parts.push(cited + " cited in report");
    } else {
      parts.push("citation usage not tracked");
    }
    return parts.join(" \u00b7 ");
  }

  function renderSources(model) {
    const host = $("context-pane-sources");
    if (!host) return;
    host.replaceChildren();
    const sources = model.sources || [];
    host.appendChild(el("p", "context-summary", sourceSummary(model)));
    if (!sources.length) {
      const detail = model.rag && model.rag.detail
        ? model.rag.detail
        : "No sources are associated with this response.";
      host.appendChild(emptyState(detail));
      return;
    }
    const list = el("ol", "context-source-list");
    sources.forEach((source) => {
      const row = el("li", "context-source");
      if (source.key != null) row.dataset.sourceKey = String(source.key);
      const head = el("div", "context-source-head");
      const title = source.url ? document.createElement("a") : el("strong", "context-source-title");
      if (source.url) {
        title.className = "context-source-title";
        title.href = source.url;
        title.target = "_blank";
        title.rel = "noopener noreferrer";
      }
      text(title, source.title || source.ref || "Source");
      head.appendChild(title);
      (source.badges || []).forEach((badge) => {
        const tone = /failed/i.test(badge) ? "warn"
          : /cited|fetched|retrieved/i.test(badge) ? "ok" : "muted";
        head.appendChild(el("span", "context-source-badge is-" + tone, badge));
      });
      row.appendChild(head);
      const meta = [
        source.type,
        source.location,
        source.score == null ? "" : "score " + Number(source.score).toFixed(2),
      ].filter(Boolean).join(" \u00b7 ");
      if (meta) row.appendChild(el("p", "context-source-meta", meta));
      if (source.excerpt) row.appendChild(el("p", "context-source-excerpt", source.excerpt));
      list.appendChild(row);
    });
    host.appendChild(list);
    focusPendingSource(host);
  }

  /* A citation marker asked for one source: mark it and bring it into view.
   * Everything here is optional so a DOM without these methods is inert. */
  function focusPendingSource(host) {
    const key = state.pendingSource;
    state.pendingSource = null;
    if (!key || typeof host.querySelectorAll !== "function") return;
    const rows = Array.from(host.querySelectorAll("[data-source-key]"));
    const row = rows.find((item) => item.dataset && item.dataset.sourceKey === key);
    if (!row) return;
    if (row.classList && row.classList.add) row.classList.add("is-target");
    if (typeof row.scrollIntoView === "function") {
      row.scrollIntoView({ block: "nearest" });
    }
  }

  function toolSummary(run) {
    const tools = run.tools || {};
    const lines = [];
    if ((tools.enabled || []).length) lines.push("Enabled: " + tools.enabled.join(", "));
    if ((tools.planned || []).length) lines.push("Planned: " + tools.planned.join(", "));
    if ((tools.plannedBlocked || []).length) lines.push("Planned but disabled: " + tools.plannedBlocked.join(", "));
    if ((tools.invoked || []).length) lines.push("Invoked: " + tools.invoked.join(", "));
    if ((tools.failed || []).length) lines.push("Failed: " + tools.failed.join(", "));
    if ((tools.skipped || []).length) lines.push("Skipped: " + tools.skipped.join("; "));
    if ((tools.selected || []).length) lines.push("Selected: " + tools.selected.join(", "));
    return lines;
  }

  function renderRun(model) {
    const host = $("context-pane-run");
    if (!host) return;
    host.replaceChildren();
    const run = model.run || {};
    const status = run.status || model.status || {};
    const top = el("div", "context-run-head");
    top.appendChild(statusChip(status));
    if (run.pattern) top.appendChild(el("span", "context-kicker", run.pattern));
    if (run.durationMs) top.appendChild(el("span", "context-kicker", root.AthenaLogic.duration(run.durationMs)));
    host.appendChild(top);

    if (run.kind === "chat") {
      const bits = [run.provider, run.model].filter(Boolean).join(" \u00b7 ");
      if (bits) host.appendChild(el("p", "context-run-meta", bits));
    }

    const toolLines = toolSummary(run);
    if (toolLines.length) {
      const box = el("div", "context-tool-box");
      toolLines.forEach((line) => box.appendChild(el("p", "context-kv", line)));
      host.appendChild(box);
    }

    const rows = run.rows || [];
    if (rows.length) {
      const list = el("ol", "context-activity-list");
      rows.forEach((row) => {
        if (root.AthenaUi && typeof root.AthenaUi.activityRow === "function") {
          list.appendChild(root.AthenaUi.activityRow(row));
        } else {
          const fallback = el("li", "context-activity-fallback");
          fallback.appendChild(el("strong", "", row.label || "activity"));
          fallback.appendChild(el("span", "", row.status || ""));
          list.appendChild(fallback);
        }
      });
      host.appendChild(list);
    } else {
      host.appendChild(emptyState("No execution events were recorded for this response."));
    }

    const failures = run.failures || [];
    if (failures.length) {
      const box = el("div", "context-warning-box");
      box.appendChild(el("div", "sidebar-label", "Failed invocations"));
      failures.forEach((failure) => box.appendChild(el("p", "context-note", failure)));
      host.appendChild(box);
    }

    const warnings = run.warnings || [];
    if (warnings.length) {
      const box = el("div", "context-warning-box");
      box.appendChild(el("div", "sidebar-label", model.kind === "research" ? "Evidence gaps / warnings" : "Warnings"));
      warnings.forEach((warning) => box.appendChild(el("p", "context-note", warning)));
      host.appendChild(box);
    }

    if (run.canOpenDetails && root.AthenaUi && typeof root.AthenaUi.showView === "function") {
      const button = el("button", "ghost btn-sm context-open-details", "Open full run details");
      button.type = "button";
      button.addEventListener("click", () => {
        close(false);
        root.AthenaUi.showView("details");
      });
      host.appendChild(button);
    }
  }

  function renderContext(model) {
    const host = $("context-pane-context");
    if (!host) return;
    host.replaceChildren();
    const context = model.context || {};
    if (context.prompt) {
      const box = el("div", "context-prompt");
      box.appendChild(el("div", "sidebar-label", "Prompt"));
      box.appendChild(el("p", "", context.prompt));
      host.appendChild(box);
    }
    const items = context.items || [];
    if (items.length) {
      const list = el("dl", "context-kv-list");
      items.forEach((item) => {
        list.appendChild(el("dt", "", item.label));
        list.appendChild(el("dd", "", item.value));
      });
      host.appendChild(list);
    }
    const notes = context.notes || [];
    if (notes.length) {
      const box = el("div", "context-notes");
      box.appendChild(el("div", "sidebar-label", "Notes and limitations"));
      notes.forEach((note) => box.appendChild(el("p", "context-note", note)));
      host.appendChild(box);
    }
    if (!context.prompt && !items.length && !notes.length) {
      host.appendChild(emptyState("No context was recorded for this response."));
    }
  }

  function render() {
    const panel = $("context-panel");
    if (!panel) return;
    const provider = state.target ? providers.get(state.target.kind) : null;
    let model = null;
    if (provider && state.target) {
      try { model = provider(state.target.id); } catch (_) { model = null; }
    }
    const title = $("context-title");
    const subtitle = $("context-subtitle");
    const status = $("context-status");
    if (!model) {
      text(title, "No response selected");
      text(subtitle, "Select a Chat response or Research answer to inspect its context.");
      if (status) { status.replaceChildren(); status.className = "context-status"; }
      ["sources", "run", "context"].forEach((tab) => {
        const host = $("context-pane-" + tab);
        if (host) {
          host.replaceChildren();
          host.appendChild(emptyState("No response is selected."));
        }
      });
    } else {
      text(title, model.title || "Response");
      text(subtitle, model.subtitle || "");
      if (status) {
        status.replaceChildren();
        status.className = "context-status";
        status.appendChild(statusChip(model.status || { label: "Recorded", tone: "muted" }));
      }
      renderSources(model);
      renderRun(model);
      renderContext(model);
    }
    document.querySelectorAll("#context-tabs .context-tab").forEach((button) => {
      const selected = button.dataset.contextTab === state.tab;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-selected", selected ? "true" : "false");
      button.tabIndex = selected ? 0 : -1;
    });
    ["sources", "run", "context"].forEach((tab) => {
      const pane = $("context-pane-" + tab);
      if (pane) pane.classList.toggle("hidden", tab !== state.tab);
    });
  }

  function handleTabKey(event) {
    const focused = document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.contextTab
      : "";
    const current = TABS.includes(focused) ? focused : state.tab;
    const index = TABS.indexOf(current);
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      setTab(TABS[(index + step + TABS.length) % TABS.length], true);
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      setTab(TABS[event.key === "Home" ? 0 : TABS.length - 1], true);
    }
  }

  function wire() {
    loadPrefs();
    const panel = $("context-panel");
    if (!panel) return;
    panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close(true);
      }
    });
    $("context-close")?.addEventListener("click", () => close(true));
    $("context-backdrop")?.addEventListener("click", () => close(true));
    $("context-tabs")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-context-tab]");
      if (button) setTab(button.dataset.contextTab);
    });
    $("context-tabs")?.addEventListener("keydown", handleTabKey);
    window.addEventListener("resize", syncShell);
    syncShell();
    if (state.open) render();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();

  root.AthenaContext = {
    register,
    open,
    openSource,
    toggle,
    close,
    setTab,
    setView,
    setSurface,
    target,
    refresh,
    isOpen: () => state.open,
    isOverlay,
    currentTab: () => state.tab,
    breakpoint: CONTEXT_BREAKPOINT,
  };
}(typeof globalThis !== "undefined" ? globalThis : this));
