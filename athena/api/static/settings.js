/* Athena Settings: a floating modal over Chat and Research. */
(function (root) {
  "use strict";

  const UI = () => root.AthenaUi;
  const L = () => root.AthenaLogic;
  const ICONS = () => root.AthenaIcons;

  const SECTIONS = [
    { id: "add-model", label: "Add model", keywords: ["provider", "preset", "key", "base", "model"] },
    { id: "models", label: "Models", keywords: ["provider", "active", "test", "delete", "edit", "order", "default"] },
    { id: "defaults", label: "AI Defaults", keywords: ["chat", "research", "default"] },
    { id: "appearance", label: "Appearance", keywords: ["theme", "dark", "light", "background", "capillary"] },
    { id: "library", label: "Library", keywords: ["rag", "documents", "index", "corpus", "research", "runs"] },
    { id: "mcp", label: "MCP", keywords: ["servers", "tools", "rag", "web", "calc"] },
    { id: "personalisation", label: "Personalisation", keywords: ["about", "me", "preferences"] },
    { id: "about", label: "About", keywords: ["version", "tour", "help"] },
  ];

  const state = {
    open: false,
    section: "add-model",
    profiles: [],
    presets: [],
    active: "",
    editing: "",
    request: { status: "idle", message: "", rows: [] },
    defaults: { chat_profile_id: "" },
    library: null,
    mcp: null,
    lastFocus: null,
    libraryTimer: null,
  };

  function el(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) node.textContent = String(value);
    return node;
  }

  function icon(name, className) {
    const factory = ICONS();
    return factory ? factory.icon(name, className || "icon sm") : el("span", "icon-fallback");
  }

  function button(label, className, iconName, onClick) {
    const node = el("button", className || "ghost btn-sm");
    node.type = "button";
    if (iconName) node.appendChild(icon(iconName));
    node.appendChild(el("span", "", label));
    if (onClick) node.addEventListener("click", onClick);
    return node;
  }

  function focusables() {
    const dialog = UI().$("settings-dialog");
    if (!dialog) return [];
    return Array.from(dialog.querySelectorAll(
      "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]"
    )).filter((node) => node.offsetParent !== null);
  }

  function trapFocus(event) {
    if (document.body.classList.contains("tutorial-active")) return;
    const nodes = focusables();
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function open(section) {
    const modal = UI().$("settings-modal");
    if (!modal) return;
    if (!state.open) {
      state.lastFocus = document.activeElement;
      modal.classList.remove("hidden");
      document.body.classList.add("modal-open");
      root.AthenaBackground?.pause();
      state.open = true;
    }
    state.section = section || state.section || "add-model";
    load().then(() => selectSection(state.section)).catch((error) => {
      UI().text(UI().$("settings-state"), "cannot load settings: " + error.message);
    });
    try { history.replaceState(null, "", "#settings"); } catch (_) { /* ignore */ }
    const nav = document.querySelector('.settings-nav-item[data-section="' + state.section + '"]');
    if (nav) nav.focus();
  }

  function close() {
    const modal = UI().$("settings-modal");
    if (!modal || !state.open) return;
    modal.classList.add("hidden");
    document.body.classList.remove("modal-open");
    state.open = false;
    if (state.libraryTimer) clearTimeout(state.libraryTimer);
    state.libraryTimer = null;
    root.AthenaBackground?.play();
    const view = UI().currentView ? UI().currentView() : "run";
    try { history.replaceState(null, "", "#" + view); } catch (_) { /* ignore */ }
    const target = state.lastFocus;
    state.lastFocus = null;
    if (target && typeof target.focus === "function") target.focus();
  }

  function selectSection(id) {
    const known = SECTIONS.some((section) => section.id === id);
    state.section = known ? id : "add-model";
    document.querySelectorAll(".settings-nav-item").forEach((item) => {
      item.classList.toggle("is-active", item.dataset.section === state.section);
    });
    document.querySelectorAll(".settings-section").forEach((pane) => {
      pane.classList.toggle("hidden", pane.dataset.pane !== state.section);
    });
    if (state.section === "library") loadLibrary();
    if (state.section === "mcp") loadMcp();
    if (state.section === "models") renderModels();
    if (state.section === "defaults") renderDefaults();
    if (state.section === "appearance") renderAppearance();
  }

  function filterNav() {
    const query = UI().$("settings-find") ? UI().$("settings-find").value : "";
    const visible = new Set(L().filterSettingsSections(SECTIONS, query));
    document.querySelectorAll(".settings-nav-item").forEach((item) => {
      item.classList.toggle("hidden", !visible.has(item.dataset.section));
    });
  }

  /* ------------------------------------------------------------- providers */

  function setRequest(action) {
    state.request = L().requestState(state.request, action);
    const busy = L().isBusy(state.request);
    ["profile-save", "profile-test", "profile-activate", "profile-delete", "profile-cancel", "default-chat-profile"].forEach((id) => {
      const node = UI().$(id);
      if (node) node.disabled = busy;
    });
    document.querySelectorAll("#provider-cards button").forEach((node) => {
      node.disabled = busy || node.dataset.orderBoundary === "true";
    });
    UI().text(UI().$("profile-status"), state.request.message);
    UI().text(UI().$("models-status"), state.request.message);
    renderTestResults();
  }

  function renderTestResults() {
    const host = UI().$("test-result");
    if (!host) return;
    host.replaceChildren();
    const rows = state.request.rows || [];
    if (!rows.length) {
      if (state.request.status === "error" || state.request.status === "timeout") {
        host.appendChild(el("p", "error-text", state.request.message));
      }
      return;
    }
    const table = el("table", "table");
    const head = document.createElement("tr");
    ["path", "ok", "host", "tokens", "error"].forEach((label) => head.appendChild(el("th", "", label)));
    table.appendChild(head);
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      [row.path, row.ok ? "ok" : "failed", row.host,
       (row.tokens_in || 0) + "/" + (row.tokens_out || 0),
       row.error_type ? row.error_type + ": " + (row.error || "") : ""].forEach((value) => {
        tr.appendChild(el("td", "", value));
      });
      table.appendChild(tr);
    });
    host.appendChild(table);
  }

  function renderPresets() {
    const select = UI().$("profile-preset");
    if (!select) return;
    const current = select.value;
    select.replaceChildren();
    const rows = state.presets.length ? state.presets : [{ id: "custom", name: "Custom / OpenAI-compatible" }];
    rows.forEach((preset) => select.appendChild(new Option(preset.name || preset.id, preset.id)));
    if (!rows.some((preset) => preset.id === "custom")) {
      select.appendChild(new Option("Custom / OpenAI-compatible", "custom"));
    }
    if (current && !Array.from(select.options).some((option) => option.value === current)) {
      select.appendChild(new Option(current, current));
    }
    select.value = current || "custom";
    select.dataset.previous = select.value;
  }

  function applyPreset(presetId, previousId) {
    const preset = state.presets.find((item) => item.id === presetId);
    if (!preset || presetId === "custom") return;
    const previous = state.presets.find((item) => item.id === previousId);
    const name = UI().$("profile-name");
    if (!name.value.trim() || (!state.editing && name.value === previous?.name)) {
      name.value = preset.name || preset.id;
    }
    const filled = L().presetFill(state.presets, presetId);
    UI().$("profile-api-base").value = filled.api_base;
    UI().$("profile-model").value = filled.model;
    clearDiscovery();
  }

  function clearDiscovery() {
    UI().$("profile-model-options").replaceChildren();
    UI().text(UI().$("profile-discovery-hint"), "");
  }

  function updateSearchEndpoint() {
    const endpoint = UI().$("profile-search-endpoint");
    endpoint.disabled = UI().$("profile-search").value !== "searxng";
    endpoint.closest("label").classList.toggle("hidden", endpoint.disabled);
  }

  function formPayload() {
    const searchEndpoint = UI().$("profile-search-endpoint");
    return {
      name: UI().$("profile-name").value.trim(),
      preset: UI().$("profile-preset").value || "custom",
      model: UI().$("profile-model").value.trim(),
      api_base: UI().$("profile-api-base").value.trim(),
      reasoning_effort: UI().$("profile-reasoning").value,
      search_provider: UI().$("profile-search").value,
      search_endpoint: searchEndpoint.disabled ? "" : searchEndpoint.value.trim(),
      llm_key: UI().$("profile-key").value.trim(),
    };
  }

  function resetForm() {
    state.editing = "";
    const title = UI().$("profile-form-title");
    if (title) UI().text(title, "Add model");
    UI().text(UI().$("profile-form-description"), "Choose a provider preset or enter an OpenAI-compatible endpoint.");
    ["profile-name", "profile-api-base", "profile-model", "profile-key", "profile-search-endpoint"].forEach((id) => {
      const node = UI().$(id);
      if (node) node.value = "";
    });
    UI().$("profile-preset").value = "custom";
    UI().$("profile-preset").dataset.previous = "custom";
    UI().$("profile-reasoning").value = "";
    UI().$("profile-search").value = "duckduckgo";
    updateSearchEndpoint();
    UI().text(UI().$("profile-key-hint"), "Your key is stored securely. Local endpoints can leave this blank.");
    UI().text(UI().$("profile-errors"), "");
    UI().text(UI().$("profile-save"), "Add model");
    ["profile-activate", "profile-delete", "profile-cancel"].forEach((id) => UI().$(id).classList.add("hidden"));
    clearDiscovery();
    setRequest({ type: "reset" });
  }

  function editProfile(profile) {
    resetForm();
    state.editing = profile.id;
    UI().text(UI().$("profile-form-title"), "Edit " + profile.name);
    UI().text(UI().$("profile-form-description"), profile.id === state.active
      ? "Reactivate the profile after saving to apply changes to Research."
      : "Update the model, endpoint, API key or search options for this profile.");
    UI().$("profile-name").value = profile.name || "";
    const preset = UI().$("profile-preset");
    const presetId = profile.preset || "custom";
    if (!Array.from(preset.options).some((option) => option.value === presetId)) {
      preset.appendChild(new Option(presetId, presetId));
    }
    preset.value = presetId;
    preset.dataset.previous = presetId;
    UI().$("profile-api-base").value = profile.api_base || "";
    UI().$("profile-model").value = profile.model || "";
    UI().text(UI().$("profile-key-hint"), profile.has_llm_key
      ? "Key stored. Leave blank to keep it, or enter a new key to replace it."
      : "No API key is stored for this profile.");
    UI().$("profile-reasoning").value = profile.reasoning_effort || "";
    UI().$("profile-search").value = profile.search_provider || "duckduckgo";
    UI().$("profile-search-endpoint").value = profile.search_endpoint || "";
    updateSearchEndpoint();
    UI().text(UI().$("profile-save"), "Save changes");
    ["profile-activate", "profile-delete", "profile-cancel"].forEach((id) => UI().$(id).classList.remove("hidden"));
    selectSection("add-model");
    UI().$("profile-name").focus();
  }

  function cancelEdit() {
    resetForm();
    selectSection("models");
  }

  async function discoverModels() {
    const payload = formPayload();
    const base = payload.api_base;
    if (!base) { UI().toast("Set the API base URL first."); return; }
    const editing = state.editing;
    const button = UI().$("profile-discover");
    button.disabled = true;
    clearDiscovery();
    UI().text(UI().$("profile-discovery-hint"), "discovering...");
    try {
      const data = await UI().api("/api/settings/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          profile_id: editing,
          api_base: base,
          llm_key: payload.llm_key || null,
        }),
      });
      if (state.editing !== editing || UI().$("profile-api-base").value.trim() !== base) return;
      const model = L().presetFill(state.presets, payload.preset).model || payload.model;
      const provider = model.includes("/") ? model.split("/", 1)[0] : "openai";
      const list = UI().$("profile-model-options");
      (data.models || []).forEach((id) => list.appendChild(new Option(id, provider + "/" + id)));
      UI().text(UI().$("profile-discovery-hint"), data.count
        ? "Found " + data.count + " models — pick one in the Model field."
        : "Model discovery unavailable for this endpoint — type the model id manually.");
      if (data.error) UI().text(UI().$("profile-discovery-hint"), data.error);
    } catch (error) {
      if (state.editing === editing && UI().$("profile-api-base").value.trim() === base) {
        UI().text(UI().$("profile-discovery-hint"), "Model discovery failed: " + error.message);
      }
    } finally {
      button.disabled = false;
    }
  }

  async function saveProfile(keepEditing = false) {
    if (L().isBusy(state.request)) return "";
    const payload = formPayload();
    const existing = state.profiles.find((item) => item.id === state.editing);
    const check = L().validateProfile({
      ...payload,
      needs_key: !existing?.has_llm_key && !L().isLocalApiBase(payload.api_base),
    });
    UI().text(UI().$("profile-errors"), check.ok ? "" : Object.values(check.errors).join("; "));
    if (!check.ok) return "";
    setRequest({ type: "start", message: "saving..." });
    payload.llm_key = payload.llm_key || null;
    try {
      const saved = state.editing
        ? await UI().api("/api/settings/profiles/" + encodeURIComponent(state.editing), {
            method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
          })
        : await UI().api("/api/settings/profiles", {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
          });
      if (keepEditing) editProfile(saved);
      else resetForm();
      setRequest({ type: "ok", message: "Saved " + saved.name, rows: [] });
      if (!keepEditing) selectSection("models");
      root.AthenaChat?.reloadModels?.();
      await load().catch((error) => UI().toast("Could not reload settings: " + error.message));
      return saved.id;
    } catch (error) {
      setRequest({ type: "error", message: "save failed: " + error.message });
      return "";
    }
  }

  async function testProfile(profileId) {
    const started = Date.now();
    setRequest({ type: "start", message: "testing... 0s" });
    const ticker = setInterval(() => {
      const seconds = Math.round((Date.now() - started) / 1000);
      state.request = { ...state.request, message: "testing... " + seconds + "s" };
      UI().text(UI().$("profile-status"), state.request.message);
    }, 1000);
    try {
      const data = await UI().api("/api/settings/profiles/" + encodeURIComponent(profileId) + "/test", {
        method: "POST",
      });
      const rows = data.results || [];
      const passed = rows.filter((row) => row.ok).length;
      setRequest({ type: passed === rows.length && passed ? "ok" : "error",
        message: passed + " of " + rows.length + " paths passed", rows });
    } catch (error) {
      setRequest(error.code === "timeout" ? { type: "timeout" } : { type: "error", message: "test failed: " + error.message });
    } finally {
      clearInterval(ticker);
    }
  }

  async function activateProfile(profileId) {
    const id = typeof profileId === "string" && profileId ? profileId : state.editing;
    if (!id) {
      setRequest({ type: "error", message: "select a saved provider or use its activate action" });
      return;
    }
    const selected = state.profiles.find((profile) => profile.id === id);
    if (!selected) {
      setRequest({ type: "error", message: "select a saved provider first" });
      return;
    }
    setRequest({ type: "start", message: "restarting crew server..." });
    try {
      const result = await UI().api("/api/settings/activate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile_id: id }),
      });
      if (result.ok) setRequest({ type: "ok", message: "crew server: up", rows: [] });
      else setRequest({ type: "error", message: "profile was not activated; crew server did not start", rows: [] });
      await load();
      if (UI().refreshHealth) UI().refreshHealth();
      root.AthenaChat?.reloadModels?.();
    } catch (error) {
      setRequest(error.code === "timeout" ? { type: "timeout" } : { type: "error", message: String(error.message) });
    }
  }

  async function deleteProfile(id) {
    if (!id) { setRequest({ type: "error", message: "nothing selected" }); return; }
    if (!await UI().confirmDelete({
      title: "Delete this model profile?",
      target: state.profiles.find((profile) => profile.id === id)?.name,
      detail: "The profile and its stored API key will be deleted.",
    })) return;
    setRequest({ type: "start", message: "deleting..." });
    try {
      await UI().api("/api/settings/profiles/" + encodeURIComponent(id), { method: "DELETE" });
      if (id === state.editing) resetForm();
      setRequest({ type: "ok", message: "deleted", rows: [] });
      await load();
      UI().refreshHealth?.();
      root.AthenaChat?.reloadModels?.();
      selectSection("models");
    } catch (error) {
      setRequest({ type: "error", message: "delete failed: " + error.message });
    }
  }

  async function moveModel(profile, offset) {
    if (L().isBusy(state.request)) return;
    const ids = state.profiles.map((item) => item.id);
    const from = ids.indexOf(profile.id);
    const to = from + offset;
    if (from < 0 || to < 0 || to >= ids.length) return;
    ids.splice(from, 1);
    ids.splice(to, 0, profile.id);
    setRequest({ type: "start", message: "saving model order..." });
    try {
      const saved = await UI().api("/api/settings/profiles/order", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile_ids: ids }),
      });
      state.profiles = saved.profiles;
      setRequest({ type: "ok", message: "Model order saved", rows: [] });
      renderModels();
      renderDefaults();
      root.AthenaChat?.reloadModels?.();
      if (state.open && state.section === "models") {
        const direction = offset < 0 ? "up" : "down";
        const moved = UI().$("model-" + direction + "-" + profile.id);
        if (moved && !moved.disabled) moved.focus();
        else UI().$("model-" + (offset < 0 ? "down" : "up") + "-" + profile.id)?.focus();
      }
    } catch (error) {
      setRequest({ type: "error", message: "Could not save model order: " + error.message });
      if (error.status === 409) await load().catch(() => {});
    }
  }

  function renderModels() {
    const host = UI().$("provider-cards");
    if (!host) return;
    host.replaceChildren();
    const table = UI().$("profiles-table");
    if (table) table.classList.add("hidden");
    const body = UI().$("profiles-body");
    if (body) body.replaceChildren();
    if (!state.profiles.length) {
      host.appendChild(el("p", "empty-state", "No models configured yet. Use Add model to connect a provider."));
      return;
    }
    const busy = L().isBusy(state.request);
    state.profiles.forEach((profile, index) => {
      const card = el("article", "provider-card");
      const head = el("div", "provider-head");
      head.appendChild(el("strong", "provider-name", profile.name));
      if (profile.id === state.active) head.appendChild(el("span", "pv-status is-ok", "active for research"));
      const local = L().isLocalApiBase(profile.api_base);
      const keyLabel = profile.has_llm_key ? "key stored" : local ? "local, no key" : "missing key";
      head.appendChild(el("span", "pv-status " + (profile.has_llm_key || local ? "is-ok" : "is-warn"), keyLabel));
      const order = el("div", "provider-order");
      for (const [direction, symbol, offset] of [["up", "↑", -1], ["down", "↓", 1]]) {
        const move = button(symbol, "ghost btn-sm", null, () => moveModel(profile, offset));
        const atBoundary = index + offset < 0 || index + offset >= state.profiles.length;
        move.id = "model-" + direction + "-" + profile.id;
        move.title = "Move " + profile.name + " " + direction;
        move.setAttribute("aria-label", move.title);
        move.dataset.orderBoundary = String(atBoundary);
        move.disabled = busy || atBoundary;
        order.appendChild(move);
      }
      head.appendChild(order);
      card.appendChild(head);
      card.appendChild(el("p", "provider-line", profile.model || "no model set"));
      const hostName = String(profile.api_base || "").replace(/^https?:\/\//, "").split("/")[0];
      card.appendChild(el("p", "provider-meta", hostName || "provider default host"));
      const meta = [
        profile.reasoning_effort ? "reasoning: " + profile.reasoning_effort : "",
        "search: " + (profile.search_provider || "duckduckgo"),
      ].filter(Boolean).join(" · ");
      card.appendChild(el("p", "provider-meta", meta));
      const actions = el("div", "provider-actions");
      actions.appendChild(button("Test", "ghost btn-sm", "play", async () => {
        editProfile(profile);
        await testProfile(profile.id);
      }));
      actions.appendChild(button("Edit", "ghost btn-sm", "edit", () => editProfile(profile)));
      const isDefault = profile.id === state.defaults.chat_profile_id;
      const makeDefault = button(isDefault ? "Chat default" : "Make Chat default", "ghost btn-sm",
        isDefault ? "check" : null, () => saveDefaults(profile.id));
      makeDefault.id = "model-default-" + profile.id;
      makeDefault.setAttribute("aria-pressed", String(isDefault));
      actions.appendChild(makeDefault);
      actions.appendChild(button("Activate for Research", "ghost btn-sm", "check", () => activateProfile(profile.id)));
      actions.appendChild(button("Delete", "danger btn-sm", "delete", () => deleteProfile(profile.id)));
      if (busy) Array.from(actions.children).forEach((node) => { node.disabled = true; });
      card.appendChild(actions);
      host.appendChild(card);
    });
  }

  /* -------------------------------------------------------------- defaults */

  function renderDefaults() {
    const select = UI().$("default-chat-profile");
    if (!select) return;
    select.replaceChildren();
    select.appendChild(new Option("Active profile (default)", ""));
    select.appendChild(new Option("Environment (.env)", "__env__"));
    state.profiles.forEach((profile) => {
      select.appendChild(new Option(profile.name + " · " + (profile.model || "no model"), profile.id));
    });
    select.value = state.defaults.chat_profile_id || "";
    const active = state.profiles.find((profile) => profile.id === state.active);
    const research = active
      ? active.name + " · " + active.model
      : (state.profiles.length ? "No active profile; environment is used." : "Environment (.env)");
    UI().text(UI().$("default-research-model"), research);
  }

  async function saveDefaults(value) {
    if (L().isBusy(state.request) || value === (state.defaults.chat_profile_id || "")) return;
    const focus = document.activeElement?.id;
    setRequest({ type: "start", message: "saving Chat default..." });
    try {
      const data = await UI().api("/api/settings/defaults", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_profile_id: value }),
      });
      state.defaults.chat_profile_id = data.chat_profile_id;
      const message = "Chat default saved";
      UI().text(UI().$("defaults-status"), message);
      setRequest({ type: "ok", message, rows: [] });
      renderDefaults();
      renderModels();
      root.AthenaChat?.reloadModels?.({ resetDraft: true });
      UI().$(focus)?.focus();
    } catch (error) {
      renderDefaults();
      const message = "Could not save Chat default: " + error.message;
      UI().text(UI().$("defaults-status"), message);
      setRequest({ type: "error", message });
    }
  }

  /* ------------------------------------------------------------- appearance */

  function renderAppearance() {
    const theme = document.documentElement.dataset.theme || "dark";
    const background = root.AthenaBackground ? root.AthenaBackground.isEnabled() : true;
    const choices = [
      ["theme-choice-dark", theme === "dark"],
      ["theme-choice-light", theme === "light"],
      ["background-on", background],
      ["background-off", !background],
    ];
    choices.forEach(([id, active]) => {
      const node = UI().$(id);
      if (node) {
        node.classList.toggle("selected", active);
        node.setAttribute("aria-pressed", String(active));
      }
    });
    const available = Boolean(root.AthenaBackground);
    ["background-on", "background-off"].forEach((id) => {
      const node = UI().$(id);
      if (node) {
        node.disabled = !available;
        node.title = available ? "" : "The background module is not available.";
      }
    });
  }

  /* ---------------------------------------------------------------- library */

  async function loadLibrary() {
    try {
      const data = await UI().api("/api/settings/library");
      state.library = data;
      renderLibrary(data);
      await renderRuns();
      if (data.reindex && data.reindex.state === "running") {
        if (state.libraryTimer) clearTimeout(state.libraryTimer);
        state.libraryTimer = setTimeout(loadLibrary, 2000);
      }
    } catch (error) {
      UI().text(UI().$("library-status"), "library unavailable: " + error.message);
    }
  }

  function renderLibrary(data) {
    const host = UI().$("library-content");
    if (!host) return;
    host.replaceChildren();
    const rag = data.rag || {};
    const rows = [
      ["corpus", rag.corpus_dir],
      ["index", rag.index_dir],
      ["collection", rag.collection],
      ["embeddings", rag.embedding_provider],
      ["chunks", String(rag.chunks == null ? "?" : rag.chunks)],
      ["index file", rag.index_exists ? "present" : "not built"],
    ];
    rows.forEach(([label, value]) => {
      const row = el("div", "settings-kv");
      row.appendChild(el("span", "settings-kv-label", label));
      row.appendChild(el("span", "settings-kv-value", value || "-"));
      host.appendChild(row);
    });
    if (rag.corpus_files && rag.corpus_files.length) {
      host.appendChild(el("div", "sidebar-label", "Corpus files"));
      rag.corpus_files.forEach((file) => {
        const row = el("div", "settings-kv");
        row.appendChild(el("span", "settings-kv-label", file.name));
        row.appendChild(el("span", "settings-kv-value", Math.round((file.bytes || 0) / 1024) + " KB"));
        host.appendChild(row);
      });
    }
    const job = data.reindex || {};
    const status = UI().$("library-status");
    if (status) {
      UI().text(status, job.state === "running"
        ? "Rebuilding… " + (job.tail || "")
        : job.state === "failed"
          ? "Rebuild failed (exit " + job.exit_code + "). " + (job.tail || "")
          : job.state === "done"
            ? "Rebuild finished (exit " + job.exit_code + ")."
            : "Index rebuild is available.");
    }
    const reindex = UI().$("library-reindex");
    if (reindex) reindex.disabled = job.state === "running";
  }

  async function renderRuns() {
    const host = UI().$("library-runs");
    if (!host) return;
    host.replaceChildren();
    try {
      const data = await UI().api("/api/runs");
      const rows = (data.runs || []).slice().sort((a, b) => String(b.started_at).localeCompare(String(a.started_at))).slice(0, 10);
      if (!rows.length) {
        host.appendChild(el("p", "empty-state", "No research runs yet."));
        return;
      }
      rows.forEach((run) => {
        const row = el("div", "library-row");
        const info = el("div", "library-info");
        info.appendChild(el("span", "library-title", run.question || run.run_id));
        info.appendChild(el("span", "library-meta", [run.pattern, run.outcome || run.critic_status].filter(Boolean).join(" · ")));
        row.appendChild(info);
        row.appendChild(button("Open", "ghost btn-sm", "run-details", () => {
          close();
          UI().openRun(run.run_id);
        }));
        host.appendChild(row);
      });
    } catch (error) {
      host.appendChild(el("p", "error-text", "Could not load research history: " + error.message));
    }
  }

  async function reindex() {
    const buttonNode = UI().$("library-reindex");
    if (buttonNode) buttonNode.disabled = true;
    UI().text(UI().$("library-status"), "starting rebuild...");
    try {
      await UI().api("/api/settings/library/reindex", { method: "POST" });
      await loadLibrary();
    } catch (error) {
      UI().text(UI().$("library-status"), "rebuild failed: " + error.message);
      if (buttonNode) buttonNode.disabled = false;
    }
  }

  /* -------------------------------------------------------------------- MCP */

  async function loadMcp() {
    const host = UI().$("mcp-content");
    if (!host) return;
    host.replaceChildren();
    try {
      const data = await UI().api("/api/settings/mcp");
      state.mcp = data;
      renderMcp(data);
    } catch (error) {
      host.appendChild(el("p", "error-text", "MCP status unavailable: " + error.message));
    }
  }

  function renderMcp(data) {
    const host = UI().$("mcp-content");
    if (!host) return;
    host.replaceChildren();
    (data.servers || []).forEach((server) => {
      const card = el("article", "provider-card mcp-card");
      const head = el("div", "provider-head");
      head.appendChild(el("strong", "provider-name", server.name));
      card.appendChild(head);
      card.appendChild(el("p", "provider-meta", server.module));
      (server.tools || []).forEach((tool) => {
        const row = el("div", "mcp-tool");
        row.appendChild(icon(tool.available ? "check" : "alert", "icon sm"));
        const body = el("div", "tool-row-body");
        body.appendChild(el("strong", "", tool.label || tool.name));
        body.appendChild(el("span", "tool-pick-desc", tool.description || ""));
        if (!tool.available && tool.reason) body.appendChild(el("span", "tool-pick-reason", tool.reason));
        row.appendChild(body);
        card.appendChild(row);
      });
      host.appendChild(card);
    });
    host.appendChild(el("p", "hint", data.note || ""));
  }

  /* ------------------------------------------------------------ personalisation */

  async function saveAboutMe() {
    try {
      const saved = await UI().api("/api/settings/about-me", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ about_me: UI().$("about-me").value.slice(0, 800) }),
      });
      UI().text(UI().$("about-status"), saved.about_me ? "saved" : "cleared");
    } catch (error) {
      UI().text(UI().$("about-status"), "failed: " + error.message);
    }
  }

  /* ------------------------------------------------------------------- load */

  async function load() {
    const [profiles, presets, about, defaults] = await Promise.all([
      UI().api("/api/settings/profiles"),
      UI().api("/api/settings/presets"),
      UI().api("/api/settings/about-me"),
      UI().api("/api/settings/defaults"),
    ]);
    state.profiles = profiles.profiles || [];
    state.active = profiles.active || "";
    state.presets = presets.presets || [];
    state.defaults = defaults;
    UI().$("about-me").value = about.about_me || "";
    renderPresets();
    renderModels();
    renderDefaults();
    renderAppearance();
    filterNav();
    const active = state.profiles.find((profile) => profile.id === state.active);
    UI().text(UI().$("settings-active-summary"), active
      ? "active: " + active.name + " · " + active.model
      : (state.profiles.length ? "no active provider" : "no provider configured"));
    UI().text(UI().$("settings-state"), state.profiles.length
      ? (state.active ? "" : "no active provider")
      : "No provider configured. Add one to chat or run live research.");
  }

  /* ------------------------------------------------------------------- wire */

  function wire() {
    const closeButton = UI().$("settings-close");
    if (closeButton) closeButton.addEventListener("click", close);
    const modal = UI().$("settings-modal");
    if (modal) modal.addEventListener("click", (event) => { if (event.target === modal) close(); });

    const nav = UI().$("settings-nav-items");
    if (nav) nav.addEventListener("click", (event) => {
      const item = event.target.closest(".settings-nav-item[data-section]");
      if (item) selectSection(item.dataset.section);
    });
    const find = UI().$("settings-find");
    if (find) find.addEventListener("input", filterNav);

    const save = UI().$("profile-save");
    if (save) save.addEventListener("click", () => saveProfile());
    const profileForm = UI().$("profile-form");
    if (profileForm) profileForm.addEventListener("submit", (event) => {
      event.preventDefault();
      saveProfile();
    });
    const test = UI().$("profile-test");
    if (test) test.addEventListener("click", async () => {
      const id = await saveProfile(true);
      if (id) await testProfile(id);
    });
    const activate = UI().$("profile-activate");
    if (activate) activate.addEventListener("click", async () => {
      const id = await saveProfile(true);
      if (id) await activateProfile(id);
    });
    const remove = UI().$("profile-delete");
    if (remove) remove.addEventListener("click", () => deleteProfile(state.editing));
    const cancel = UI().$("profile-cancel");
    if (cancel) cancel.addEventListener("click", cancelEdit);
    const discover = UI().$("profile-discover");
    if (discover) discover.addEventListener("click", discoverModels);
    const preset = UI().$("profile-preset");
    if (preset) preset.addEventListener("change", () => {
      applyPreset(preset.value, preset.dataset.previous);
      preset.dataset.previous = preset.value;
    });
    UI().$("profile-api-base")?.addEventListener("input", clearDiscovery);
    const search = UI().$("profile-search");
    if (search) search.addEventListener("change", updateSearchEndpoint);
    if (profileForm) resetForm();

    const defaults = UI().$("default-chat-profile");
    if (defaults) defaults.addEventListener("change", () => saveDefaults(defaults.value));

    const themeDark = UI().$("theme-choice-dark");
    if (themeDark) themeDark.addEventListener("click", () => { UI().applyTheme("dark"); renderAppearance(); });
    const themeLight = UI().$("theme-choice-light");
    if (themeLight) themeLight.addEventListener("click", () => { UI().applyTheme("light"); renderAppearance(); });
    const backgroundOn = UI().$("background-on");
    if (backgroundOn) backgroundOn.addEventListener("click", () => {
      root.AthenaBackground?.setEnabled(true);
      renderAppearance();
    });
    const backgroundOff = UI().$("background-off");
    if (backgroundOff) backgroundOff.addEventListener("click", () => {
      root.AthenaBackground?.setEnabled(false);
      renderAppearance();
    });

    const libraryRefresh = UI().$("library-refresh");
    if (libraryRefresh) libraryRefresh.addEventListener("click", loadLibrary);
    const libraryReindex = UI().$("library-reindex");
    if (libraryReindex) libraryReindex.addEventListener("click", reindex);
    const mcpRefresh = UI().$("mcp-refresh");
    if (mcpRefresh) mcpRefresh.addEventListener("click", loadMcp);
    const aboutSave = UI().$("about-save");
    if (aboutSave) aboutSave.addEventListener("click", saveAboutMe);
    const restart = UI().$("tutorial-restart");
    if (restart) restart.addEventListener("click", () => root.AthenaTutorial?.start({ manual: true }));

    document.addEventListener("keydown", (event) => {
      if (!state.open) return;
      if (event.key === "Escape" && !document.body.classList.contains("tutorial-active")) {
        event.preventDefault();
        close();
      }
      if (event.key === "Tab") trapFocus(event);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();

  root.AthenaSettings = {
    open,
    close,
    reload: load,
    isOpen: () => state.open,
    currentSection: () => state.section,
  };
}(typeof globalThis !== "undefined" ? globalThis : this));
