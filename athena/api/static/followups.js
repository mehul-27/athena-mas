(function () {
  "use strict";

  const { $, api, confirmDelete } = window.AthenaUi;
  const dialog = $("followups");
  const status = $("followups-status");
  const actionForm = $("followups-action-form");
  const actionKinds = { note: "Note", email_draft: "Email draft", calendar_event: "Calendar event" };
  const nav = [...dialog.querySelectorAll("[data-followup-view]")];
  const endpoints = { reminders: "/api/personal/reminders", watchlists: "/api/personal/watchlists", actions: "/api/actions" };
  let source = "";
  let view = "reminders";

  function element(tag, content, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content) node.textContent = content;
    return node;
  }

  function message(value = "", error = false) {
    status.classList.toggle("is-error", error);
    status.textContent = value;
  }

  function dateLabel(value) {
    const date = new Date(value);
    return value && !Number.isNaN(date.getTime()) ? date.toLocaleString() : "";
  }

  function stamp(value) {
    const date = new Date(value);
    if (!value || Number.isNaN(date.getTime())) throw new Error("Enter a valid date and time.");
    return date.toISOString();
  }

  function send(path, method, payload, timeout) {
    const options = { method };
    if (payload) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(payload);
    }
    return api(path, options, timeout);
  }

  function button(label, perform, style = "ghost") {
    const node = element("button", label, style);
    node.type = "button";
    node.addEventListener("click", () => work(node, perform));
    return node;
  }

  async function work(control, perform) {
    control.disabled = true;
    message();
    try { await perform(); }
    catch (error) { message(error.message, true); }
    finally { control.disabled = false; }
  }

  function row(title, metadata, due = false) {
    const item = element("li", "", "followups-row");
    item.append(element("strong", title), element("span", metadata, "followups-meta" + (due ? " is-due" : "")));
    return item;
  }

  function rowActions(...controls) {
    const box = element("div", "", "followups-row-actions");
    box.append(...controls);
    return box;
  }

  function renderList(host, rows, render, empty) {
    host.replaceChildren(...(rows.length ? rows.map(render) : [row(empty, "")]));
  }

  function renderReminder(reminder) {
    const path = `${endpoints.reminders}/${encodeURIComponent(reminder.id)}`;
    const due = reminder.completed ? "Completed" : (reminder.overdue ? "Due" : "Upcoming");
    const item = row(reminder.title, `${due} · ${dateLabel(reminder.due_at)}`, reminder.overdue);
    if (reminder.notes) item.append(element("p", reminder.notes));
    item.append(rowActions(button(reminder.completed ? "Reopen" : "Mark done", async () => {
      await send(path, "PATCH", { completed: !reminder.completed });
      await refresh();
    }), button("Remove", async () => {
      if (!await confirmDelete({ title: "Remove reminder?", target: reminder.title, action: "Remove" })) return;
      await send(path, "DELETE");
      await refresh();
    })));
    return item;
  }

  function changeRow(change) {
    const item = element("li");
    const label = `${change.kind === "updated" ? "Updated" : "New"} · ${change.title || change.url}`;
    let url;
    try { url = new URL(change.url); } catch (_) { url = null; }
    if (url && ["http:", "https:"].includes(url.protocol)) {
      const link = element("a", label);
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.append(link);
    } else {
      item.append(element("span", label));
    }
    item.append(element("p", dateLabel(change.created_at), "followups-meta"));
    return item;
  }

  function renderWatch(watch) {
    const path = `${endpoints.watchlists}/${encodeURIComponent(watch.id)}`;
    const checked = watch.last_checked_at ? `Last checked ${dateLabel(watch.last_checked_at)}` : "Waiting for first check";
    const item = row(watch.query, checked);
    if (watch.next_check_at) item.append(element("p", `Next check ${dateLabel(watch.next_check_at)}`, "followups-meta"));
    if (watch.change_count) {
      const details = element("details");
      details.append(element("summary", `${watch.change_count} ${watch.change_count === 1 ? "change" : "changes"}`));
      const list = element("ul", "", "followups-changes");
      list.append(...(watch.recent_changes || []).map(changeRow));
      details.append(list);
      if (watch.change_count > list.childElementCount) {
        details.append(button("Show recent history", async () => {
          const result = await api(`${path}/changes`);
          list.replaceChildren(...result.changes.map(changeRow));
        }));
      }
      item.append(details);
    }
    item.append(rowActions(button("Check now", async () => {
      message("Checking for changes…");
      const { check } = await send(`${path}/check`, "POST", null, 85000);
      await refresh();
      message(check.change_count ? `Changes recorded: ${check.change_count}` : "Check complete. No changes found.");
    }), button("Remove", async () => {
      if (!await confirmDelete({ title: "Remove watch?", target: watch.query, action: "Remove" })) return;
      await send(path, "DELETE");
      await refresh();
    })));
    return item;
  }

  function renderAction(action) {
    const path = `${endpoints.actions}/${encodeURIComponent(action.id)}`;
    const state = { pending: "Waiting for review", approved: "Approved", canceled: "Canceled" };
    const item = row(action.title, `${actionKinds[action.kind]} · ${state[action.status]}`);
    if (action.source) item.append(element("p", action.source, "followups-meta"));
    if (action.start_at) item.append(element("p", `${dateLabel(action.start_at)} – ${dateLabel(action.end_at)}`, "followups-meta"));
    const preview = element("details");
    preview.open = action.status === "pending";
    preview.append(element("summary", "Preview draft"), element("div", action.content, "followups-draft"));
    item.append(preview);
    if (action.status === "pending") {
      const transition = (verb) => async () => {
        await send(`${path}/${verb}`, "POST");
        await refresh();
      };
      preview.append(rowActions(
        button("Approve draft", transition("approve"), "primary"),
        button("Cancel draft", transition("cancel")),
      ));
    } else if (action.status === "approved") {
      const download = element("a", action.kind === "calendar_event" ? "Download calendar file" : "Download Markdown", "ghost");
      download.href = `${path}/export`;
      download.download = "";
      item.append(rowActions(download));
    }
    return item;
  }

  function updateCount(reminders, actions) {
    const count = reminders.filter((item) => item.overdue).length + actions.filter((item) => item.status === "pending").length;
    const badge = $("followups-count");
    badge.textContent = count;
    badge.classList.toggle("hidden", !count);
    $("followups-open").setAttribute("aria-label", count ? `Follow-ups, ${count} need attention` : "Follow-ups");
  }

  async function refresh() {
    const [{ reminders }, { actions }] = await Promise.all([api(endpoints.reminders), api(endpoints.actions)]);
    updateCount(reminders, actions);
    if (!dialog.open) return;
    renderList($("followups-reminder-list"), [...reminders].sort((a, b) => Number(a.completed) - Number(b.completed)), renderReminder, "No reminders yet.");
    renderList($("followups-action-list"), actions, renderAction, "No action drafts yet.");
    if (view === "watchlists") {
      const { watchlists } = await api(endpoints.watchlists);
      renderList($("followups-watch-list"), watchlists, renderWatch, "No watchlists yet.");
    }
  }

  function refreshVisible() {
    refresh().catch((error) => { if (dialog.open) message(error.message, true); });
  }

  function selectView(name) {
    view = name;
    for (const control of nav) {
      const selected = control.dataset.followupView === name;
      control.setAttribute("aria-pressed", String(selected));
      $(control.getAttribute("aria-controls")).hidden = !selected;
    }
    message();
    refreshVisible();
  }

  function bindForm(id, submit) {
    const form = $(id);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const data = Object.fromEntries(new FormData(form));
      work(event.submitter || form.querySelector("[type=submit]"), async () => {
        await submit(data);
        form.reset();
        if (form === actionForm) { setSource(""); showCalendar(false); }
        await refresh();
      });
    });
  }

  function showCalendar(show) {
    $("followups-calendar-fields").hidden = !show;
    for (const name of ["start_at", "end_at"]) {
      actionForm.elements[name].required = show;
      actionForm.elements[name].disabled = !show;
    }
  }

  function setSource(value) {
    source = value;
    const label = $("followups-action-source");
    label.hidden = !value;
    label.textContent = value;
  }

  bindForm("followups-reminder-form", (data) => send(endpoints.reminders, "POST", { title: data.title.trim(), notes: data.notes.trim(), due_at: stamp(data.due_at) }));
  bindForm("followups-watch-form", (data) => send(endpoints.watchlists, "POST", { query: data.query.trim() }));
  bindForm("followups-action-form", (data) => {
    const body = { kind: data.kind, title: data.title.trim(), content: data.content.trim(), source };
    if (data.kind === "calendar_event") {
      body.start_at = stamp(data.start_at);
      body.end_at = stamp(data.end_at);
    }
    return send(endpoints.actions, "POST", body);
  });
  actionForm.elements.kind.addEventListener("change", () => showCalendar(actionForm.elements.kind.value === "calendar_event"));
  showCalendar(false);
  nav.forEach((node) => node.addEventListener("click", () => selectView(node.dataset.followupView)));
  $("followups-open").addEventListener("click", () => { dialog.showModal(); selectView(view); });

  window.AthenaFollowups = {
    open(pane = view) {
      if (!dialog.open) dialog.showModal();
      selectView(pane);
    },
    draft({ title, content, source: origin }) {
      actionForm.reset();
      showCalendar(false);
      actionForm.elements.title.value = title.slice(0, 160);
      actionForm.elements.content.value = content.slice(0, 12000);
      setSource(origin);
      if (!dialog.open) dialog.showModal();
      selectView("actions");
      actionForm.elements.kind.focus();
    },
  };

  refreshVisible();
  window.setInterval(() => { if (!document.hidden) refreshVisible(); }, 60000);
})();
