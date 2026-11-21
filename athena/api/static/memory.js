(function () {
  "use strict";

  const { $, api, confirmDelete } = window.AthenaUi;
  const root = "/api/memory";
  const form = $("memory-form");
  const learning = $("memory-learning");
  const labels = { about_me: "About me", preference: "Preference", ongoing_goal: "Ongoing goal" };
  let editing = "";

  function status(text = "") { $("memory-status").textContent = text; }
  function write(path, method, data) {
    return api(path, { method, headers: { "Content-Type": "application/json" },
      body: data ? JSON.stringify(data) : undefined });
  }
  function edit(item) {
    editing = item?.id || "";
    form.elements.category.value = item?.category || "preference";
    form.elements.content.value = item?.content || "";
    $("memory-save").textContent = item ? "Save changes" : "Add memory";
    $("memory-cancel").classList.toggle("hidden", !item);
    if (item) form.elements.content.focus();
  }
  function button(label, action) {
    const node = document.createElement("button");
    node.className = "ghost btn-sm";
    node.type = "button";
    node.textContent = label;
    node.addEventListener("click", action);
    return node;
  }

  async function load() {
    try {
      const { items, enabled } = await api(root);
      learning.checked = enabled;
      const list = $("memory-list");
      list.replaceChildren();
      for (const item of items) {
        const row = document.createElement("li");
        row.className = "followups-row";
        const type = document.createElement("span");
        type.className = "followups-meta";
        type.textContent = labels[item.category] + (item.source === "learned" ? " · Learned from chat" : "");
        const content = document.createElement("p");
        content.textContent = item.content;
        const actions = document.createElement("div");
        actions.className = "followups-row-actions";
        actions.append(button("Edit", () => edit(item)), button("Forget", async (event) => {
          const accepted = await confirmDelete({ title: "Forget this memory?", target: item.content,
            action: "Forget", trigger: event.currentTarget });
          if (!accepted) return;
          try {
            await write(root + "/" + encodeURIComponent(item.id), "DELETE");
            if (editing === item.id) edit();
            await load();
          } catch (error) { status(error.message); }
        }));
        row.append(type, content, actions);
        list.append(row);
      }
      if (!items.length) {
        const empty = document.createElement("li");
        empty.className = "followups-row";
        empty.textContent = "No saved memories yet.";
        list.append(empty);
      }
    } catch (error) { status(error.message); }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const save = $("memory-save");
    save.disabled = true;
    try {
      await write(editing ? root + "/" + encodeURIComponent(editing) : root, editing ? "PATCH" : "POST",
        { category: form.elements.category.value, content: form.elements.content.value.trim() });
      edit();
      status("Memory saved.");
      await load();
    } catch (error) { status(error.message); }
    finally { save.disabled = false; }
  });
  $("memory-cancel").addEventListener("click", () => edit());
  learning.addEventListener("change", async () => {
    const enabled = learning.checked;
    learning.disabled = true;
    try {
      await write(root + "/settings", "PUT", { enabled });
      status(enabled ? "Automatic learning enabled." : "Automatic learning paused.");
    } catch (error) {
      learning.checked = !enabled;
      status(error.message);
    } finally { learning.disabled = false; }
  });
  window.AthenaMemory = { load };
})();
