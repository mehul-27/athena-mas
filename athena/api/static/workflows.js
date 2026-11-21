/* Saved, user-triggered prompt recipes. A workflow only fills Chat; sending is explicit. */
"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  let workflows = [];
  let editing = "";
  let draft = {};

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch (_) { /* no JSON body */ }
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return response.status === 204 ? null : response.json();
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function ensureDialog() {
    let dialog = byId("workflows");
    if (!dialog) return null;
    dialog.setAttribute("aria-labelledby", "workflow-title");
    if (!dialog.querySelector("[data-workflow-app]")) {
      const root = el("div", "followups-pane");
      root.dataset.workflowApp = "";
      dialog.replaceChildren(root);
    }
    return dialog;
  }

  function showError(error) {
    const status = document.querySelector("[data-workflow-status]");
    if (status) status.textContent = error.message || "Workflow request failed.";
  }

  function render() {
    const dialog = ensureDialog();
    const root = dialog?.querySelector("[data-workflow-app]");
    if (!root) return;
    root.replaceChildren();

    const header = el("div", "card-head");
    const title = el("h2", "", "Saved workflows");
    title.id = "workflow-title";
    header.append(title);
    const close = el("button", "ghost btn-sm", "Close");
    close.type = "button";
    close.addEventListener("click", () => dialog.close());
    header.append(close);
    root.append(header);
    root.append(el("p", "muted", "Reusable prompt recipes. Use one to fill the current Chat; review it and press Send yourself."));

    const list = el("div", "provider-cards");
    if (!workflows.length) list.append(el("p", "empty-state", "No saved workflows yet."));
    for (const workflow of workflows) {
      const card = el("article", "provider-card");
      card.append(el("strong", "provider-name", workflow.name));
      if (workflow.description) card.append(el("p", "provider-meta", workflow.description));
      const preview = workflow.prompt.length > 240 ? `${workflow.prompt.slice(0, 237)}…` : workflow.prompt;
      card.append(el("p", "provider-line", preview));
      const actions = el("div", "provider-actions");
      const use = el("button", "", "Use in chat");
      use.type = "button";
      use.addEventListener("click", () => useWorkflow(workflow));
      const edit = el("button", "", "Edit");
      edit.type = "button";
      edit.addEventListener("click", () => { editing = workflow.id; draft = {}; render(); });
      const remove = el("button", "", "Delete");
      remove.type = "button";
      remove.addEventListener("click", async () => {
        if (!await window.AthenaUi.confirmDelete({
          title: "Delete workflow?", target: workflow.name, trigger: remove,
        })) return;
        try {
          await request(`/api/workflows/${encodeURIComponent(workflow.id)}`, { method: "DELETE" });
          await load();
        } catch (error) { showError(error); }
      });
      actions.append(use, edit, remove);
      card.append(actions);
      list.append(card);
    }
    root.append(list);

    const form = el("form", "form-grid");
    const heading = el("h3", "wide", editing ? "Edit workflow" : "New workflow");
    form.append(heading);
    const current = workflows.find((item) => item.id === editing) || draft;
    const field = (label, tag, value, wide = false) => {
      const wrap = el("label", wide ? "wide" : "", label);
      const input = document.createElement(tag);
      input.value = value || "";
      input.required = label !== "Description";
      if (tag === "textarea") input.rows = label === "Prompt" ? 5 : 2;
      wrap.append(input);
      form.append(wrap);
      return input;
    };
    const name = field("Name", "input", current.name);
    name.maxLength = 100;
    const description = field("Description", "input", current.description);
    description.maxLength = 500;
    const prompt = field("Prompt recipe", "textarea", current.prompt, true);
    prompt.maxLength = 8000;
    prompt.placeholder = "Example: Compare {{input}} across recent sources. Summarize the evidence and cite links.";
    const help = el("p", "muted wide", "Use {{input}} as a placeholder for a question you provide each time.");
    form.append(help);
    const submit = el("button", "", editing ? "Save changes" : "Save workflow");
    submit.type = "submit";
    const cancel = el("button", "", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", () => { editing = ""; draft = {}; render(); });
    const actions = el("div", "provider-actions wide");
    actions.append(submit);
    if (editing || draft.prompt) actions.append(cancel);
    form.append(actions);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = { name: name.value, description: description.value, prompt: prompt.value };
      try {
        await request(editing ? `/api/workflows/${encodeURIComponent(editing)}` : "/api/workflows", {
          method: editing ? "PATCH" : "POST", body: JSON.stringify(payload),
        });
        editing = "";
        draft = {};
        await load();
      } catch (error) { showError(error); }
    });
    root.append(form);
    const status = el("p", "muted wide");
    status.dataset.workflowStatus = "";
    status.setAttribute("role", "status");
    root.append(status);
  }

  async function useWorkflow(workflow) {
    let text = workflow.prompt;
    if (text.includes("{{input}}")) {
      const input = window.prompt("What should this workflow work on?");
      if (input === null) return;
      text = text.replaceAll("{{input}}", input.trim());
    }
    if (window.AthenaChat?.prefill) {
      if (!window.AthenaChat.prefill(text)) return;
    }
    else {
      const composer = byId("chat-input");
      if (!composer) { showError(new Error("Open Chat before using a workflow.")); return; }
      composer.value = text;
      composer.dispatchEvent(new Event("input", { bubbles: true }));
      composer.focus();
    }
    ensureDialog()?.close();
  }

  async function load() {
    try {
      const result = await request("/api/workflows");
      workflows = result.workflows || [];
      render();
    } catch (error) { showError(error); }
  }

  function wire() {
    const open = byId("workflows-open");
    const dialog = ensureDialog();
    if (!open || !dialog) return;
    const show = (data = {}) => {
      editing = "";
      draft = data;
      render();
      load();
      if (!dialog.open) dialog.showModal();
    };
    open.addEventListener("click", () => show());
    window.AthenaWorkflows = {
      capture(title, content) {
        show({ name: title.slice(0, 100), prompt: ("Follow this procedure for {{input}}:\n\n" + content).slice(0, 8000) });
      },
    };
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
