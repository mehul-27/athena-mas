(function () {
  "use strict";

  const { $, api, toast, confirmDelete, showView, openRun } = window.AthenaUi;
  const root = "/api/workspaces";
  const dialog = $("workspaces");
  const picker = $("workspace-picker");
  const form = $("workspace-form");
  const linkForm = $("workspace-link");
  const labels = {
    chat: "Chat", report: "Research report", document: "Document",
    reminder: "Reminder", watchlist: "Watchlist", action: "Reviewable action",
  };
  const sources = {
    chat: ["/api/chat/conversations?limit=200", (data) => data.conversations.map((row) => [row.id, row.title])],
    report: ["/api/runs", (data) => data.runs.map((row) => [row.run_id, row.question || "Research run"])],
    document: ["/api/settings/library", (data) => data.rag.documents.map((row) => [row.doc_id || row.id, row.name])],
    reminder: ["/api/personal/reminders", (data) => data.reminders.map((row) => [row.id, row.title])],
    watchlist: ["/api/personal/watchlists", (data) => data.watchlists.map((row) => [row.id, row.query])],
    action: ["/api/actions", (data) => data.actions.map((row) => [row.id, row.title])],
  };
  let active = "";
  let projects = [];
  let editing = "";
  let revision = 0;
  let sourceRevision = 0;
  try { active = localStorage.getItem("athena.workspace") || ""; } catch (_) { /* optional */ }

  function write(path, method, body) {
    return api(path, { method, headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined });
  }

  function projectPath(id = active) { return root + "/" + encodeURIComponent(id); }
  function status(text = "", failed = false) {
    $("workspaces-status").textContent = text;
    $("workspaces-status").classList.toggle("is-error", failed);
  }
  function handle(error) { status(error.message, true); }
  function option(value, label) { return new Option(label, value); }
  function button(label, action) {
    const item = document.createElement("button");
    item.className = "ghost";
    item.type = "button";
    item.textContent = label;
    item.addEventListener("click", action);
    return item;
  }

  function select(id) {
    active = id;
    try { localStorage.setItem("athena.workspace", id); } catch (_) { /* optional */ }
    const project = projects.find((row) => row.id === id);
    $("workspace-current").textContent = project ? project.name : "Projects";
    $("workspace-current").title = project ? "Projects · " + project.name : "Projects";
    picker.value = id;
    $("workspace-detail").hidden = !project;
    window.AthenaChat?.refresh();
  }

  function switchChatScope() {
    const chat = window.AthenaChat;
    const current = chat?.state().conversation;
    if (active && current && current.workspace_id !== active && !chat.isStreaming() &&
        !$("chat-input").value.trim()) chat.newConversation();
  }

  async function loadProjects() {
    const { workspaces } = await api(root);
    projects = workspaces;
    picker.replaceChildren(option("", "All chats"), ...projects.map((row) => option(row.id, row.name)));
    select(projects.some((row) => row.id === active) ? active : "");
  }

  function openItem(item) {
    if (window.AthenaChat?.isStreaming() || window.AthenaUi.isBusy()) {
      toast("Finish the current run before opening another item.");
      return;
    }
    dialog.close();
    if (item.kind === "chat") {
      showView("chat");
      window.AthenaChat?.openConversation(item.ref);
    } else if (item.kind === "report") {
      openRun(item.ref);
    } else if (item.kind === "document") {
      showView("documents");
      window.AthenaUi.openDocPreview(item.ref, $("workspaces-open"));
    } else {
      const pane = { reminder: "reminders", watchlist: "watchlists", action: "actions" }[item.kind];
      window.AthenaFollowups?.open(pane);
    }
  }

  async function loadItems() {
    const token = ++revision;
    const id = active;
    const list = $("workspace-items");
    list.replaceChildren();
    if (!id) return;
    const { items } = await api(projectPath(id) + "/items");
    if (token !== revision || active !== id) return;
    for (const item of items) {
      const row = document.createElement("li");
      row.className = "followups-row";
      const actions = document.createElement("div");
      actions.className = "followups-row-actions";
      const caption = document.createElement("span");
      caption.className = "followups-meta";
      caption.textContent = labels[item.kind];
      actions.append(button(item.label || labels[item.kind], () => openItem(item)));
      actions.append(button("Remove", async () => {
        try {
          await write(projectPath(id) + "/items/" + item.kind + "/" + encodeURIComponent(item.ref), "DELETE");
          await loadItems();
          window.AthenaChat?.refresh();
        } catch (error) { handle(error); }
      }));
      row.append(caption, actions);
      list.append(row);
    }
    if (!items.length) {
      const empty = document.createElement("li");
      empty.className = "followups-row";
      empty.textContent = "Add an existing item or start a project chat.";
      list.append(empty);
    }
  }

  async function loadSources() {
    const token = ++sourceRevision;
    const [path, extract] = sources[linkForm.elements.kind.value];
    const host = linkForm.elements.ref;
    host.disabled = true;
    linkForm.querySelector("button").disabled = true;
    host.replaceChildren(option("", "Loading…"));
    const items = extract(await api(path)).filter(([id]) => id);
    if (token !== sourceRevision) return;
    host.replaceChildren(option("", items.length ? "Choose an item" : "No saved items"),
      ...items.map(([id, label]) => option(id, label || id)));
    host.disabled = !items.length;
    linkForm.querySelector("button").disabled = !items.length;
  }

  function showForm(project) {
    editing = project?.id || "";
    form.elements.name.value = project?.name || "";
    form.elements.notes.value = project?.notes || "";
    form.hidden = false;
    form.elements.name.focus();
  }

  $("workspaces-open").addEventListener("click", async () => {
    dialog.showModal();
    status();
    form.hidden = true;
    try {
      await loadProjects();
      await Promise.all([loadItems(), active && loadSources()]);
    } catch (error) { handle(error); }
  });
  picker.addEventListener("change", () => {
    status();
    form.hidden = true;
    select(picker.value);
    switchChatScope();
    Promise.all([loadItems(), active && loadSources()]).catch(handle);
  });
  $("workspace-new").addEventListener("click", () => showForm());
  $("workspace-edit").addEventListener("click", () => showForm(projects.find((row) => row.id === active)));
  $("workspace-cancel").addEventListener("click", () => { form.hidden = true; });
  $("workspace-chat").addEventListener("click", () => {
    if (window.AthenaChat?.isStreaming() || window.AthenaUi.isBusy()) {
      toast("Finish the current run before starting a new chat.");
      return;
    }
    dialog.close();
    showView("chat");
    window.AthenaChat?.newConversation();
  });
  $("workspace-delete").addEventListener("click", async (event) => {
    const id = active;
    const project = projects.find((row) => row.id === id);
    if (!project || !await confirmDelete({ title: "Delete project?", target: project.name,
      detail: "Chats, reports, and other items stay in Athena.", trigger: event.currentTarget })) return;
    try {
      await write(projectPath(id), "DELETE");
      await loadProjects();
      await loadItems();
    } catch (error) { handle(error); }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      const { workspace } = await write(editing ? projectPath(editing) : root, editing ? "PATCH" : "POST",
        { name: form.elements.name.value, notes: form.elements.notes.value });
      active = workspace.id;
      form.hidden = true;
      await loadProjects();
      switchChatScope();
      await Promise.all([loadItems(), loadSources()]);
    } catch (error) { handle(error); }
    finally { submit.disabled = false; }
  });
  linkForm.elements.kind.addEventListener("change", () => loadSources().catch(handle));
  linkForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const ref = linkForm.elements.ref;
    if (!ref.value || !active) return;
    try {
      await write(projectPath() + "/items", "POST", {
        kind: linkForm.elements.kind.value, ref: ref.value, label: ref.selectedOptions[0].textContent.slice(0, 240),
      });
      ref.value = "";
      await loadItems();
      window.AthenaChat?.refresh();
    } catch (error) { handle(error); }
  });

  window.AthenaWorkspaces = { activeId: () => active };
  if (active) loadProjects().catch(() => {});
})();
