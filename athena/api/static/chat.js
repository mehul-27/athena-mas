/* Athena Chat: conversations, streaming, explicit grounding and selected tools. */
(function (root) {
  "use strict";

  const UI = () => root.AthenaUi;
  const L = () => root.AthenaLogic;
  const ICONS = () => root.AthenaIcons;
  const CAPS_KEY = "athena.chat.caps";
  const TOOLS_KEY = "athena.chat.tools";
  const SCROLL_KEY = "athena.chat.scroll.v1";
  const SESSION_KEY = "athena.chat.session.v1";
  const REASONING_OPTIONS = ["off", "low", "medium", "high"];
  const savedChat = loadChatSession();

  const state = {
    active: false,
    loaded: false,
    loading: null,
    conversationId: null,
    conversation: null,
    messages: [],
    profileId: savedChat.conversationId ? "" : savedChat.profileId,
    reasoningEffort: savedChat.reasoningEffort,
    models: [],
    defaultModel: null,
    modelRequest: 0,
    modelSaving: false,
    capabilities: new Set(loadList(CAPS_KEY)),
    tools: new Set(loadList(TOOLS_KEY)),
    toolCatalog: [],
    capabilityRequest: 0,
    toolRequest: 0,
    streaming: false,
    controller: null,
    streamEls: null,
    empty: null,
    traceOpen: new Set(),
    traceClosed: new Set(),
    scrollMap: {},
    restoreScroll: null,
    scrollTimer: null,
    navToken: 0,
    listRequest: 0,
  };

  function emptyNode() {
    return state.empty || UI().$("chat-empty");
  }

  function loadList(key) {
    try {
      const raw = JSON.parse(localStorage.getItem(key) || "[]");
      return Array.isArray(raw) ? raw.filter((value) => typeof value === "string") : [];
    } catch (_) { return []; }
  }

  function saveList(key, values) {
    try { localStorage.setItem(key, JSON.stringify([...values])); } catch (_) { /* private mode */ }
  }

  function loadChatSession() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "{}");
      return {
        conversationId: typeof saved?.conversationId === "string" ? saved.conversationId : "",
        profileId: typeof saved?.profileId === "string" ? saved.profileId : "",
        reasoningEffort: REASONING_OPTIONS.includes(saved?.reasoningEffort) ? saved.reasoningEffort : "",
      };
    } catch (_) { return { conversationId: "", profileId: "", reasoningEffort: "" }; }
  }

  function rememberChat() {
    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify({
        conversationId: state.conversationId,
        profileId: state.conversationId ? "" : state.profileId,
        reasoningEffort: state.reasoningEffort,
      }));
    } catch (_) { /* private mode */ }
  }

  function loadScrollMap() {
    try {
      const raw = JSON.parse(localStorage.getItem(SCROLL_KEY) || "{}");
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
      const clean = {};
      Object.keys(raw).forEach((key) => {
        const value = Number(raw[key]);
        if (Number.isFinite(value) && value >= 0) clean[key] = value;
      });
      return clean;
    } catch (_) {
      return {};
    }
  }

  function saveScrollMap() {
    try { localStorage.setItem(SCROLL_KEY, JSON.stringify(state.scrollMap)); } catch (_) { /* private mode */ }
  }

  function saveCurrentScroll() {
    const main = UI().$("main");
    if (!main || !state.conversationId) return;
    state.scrollMap[state.conversationId] = Math.max(0, Math.round(main.scrollTop));
    saveScrollMap();
  }

  function restoreAfterRender() {
    if (state.restoreScroll == null) {
      scrollToBottom();
      return;
    }
    const top = state.restoreScroll;
    state.restoreScroll = null;
    requestAnimationFrame(() => {
      const main = UI().$("main");
      if (!main) return;
      const max = Math.max(0, main.scrollHeight - main.clientHeight);
      main.scrollTop = Math.min(top, max);
    });
  }

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

  function relative(value) {
    if (!value) return "";
    const stamp = Date.parse(value);
    if (!Number.isFinite(stamp)) return "";
    const seconds = Math.max(0, Math.round((Date.now() - stamp) / 1000));
    if (seconds < 60) return "just now";
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + "m ago";
    const hours = Math.round(minutes / 60);
    if (hours < 24) return hours + "h ago";
    return Math.round(hours / 24) + "d ago";
  }

  function isAvailable(item) {
    return Boolean(item && item.available);
  }

  /* ------------------------------------------------------------- rendering */

  function renderTranscript() {
    const host = UI().$("chat-transcript");
    if (!host) return;
    host.replaceChildren();
    state.streamEls = null;
    if (!state.messages.length) {
      host.appendChild(emptyNode());
      return;
    }
    state.messages.forEach((message, index) => host.appendChild(renderMessage(message, index)));
    const last = state.messages[state.messages.length - 1];
    if (last && last.role === "assistant" && last.status === "streaming") {
      const node = host.lastElementChild;
      const textNode = node && node.querySelector(".chat-text, .chat-typing");
      state.streamEls = { text: textNode };
    }
    restoreAfterRender();
  }

  function userBefore(index) {
    for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
      if (state.messages[cursor].role === "user") return state.messages[cursor];
    }
    return null;
  }

  function messageModel(message, index) {
    return L().chatContextModel({
      message,
      userMessage: userBefore(index),
      conversation: state.conversation,
    });
  }

  function responseMeta(message, model) {
    const row = el("div", "response-meta chat-response-meta");
    L().responseMetaFromModel(model).forEach((item) => {
      const button = el("button", "response-meta-pill is-" + (item.tone || "muted"), item.label);
      button.type = "button";
      button.title = item.title || item.label;
      button.dataset.contextAction = item.action || "context";
      button.addEventListener("click", () => {
        root.AthenaContext?.open(item.action || "context", "chat", message.id, button);
      });
      row.appendChild(button);
    });
    return row;
  }

  function contextModel(id) {
    const message = state.messages.find((item) => item.id === id && item.role === "assistant");
    if (!message) return null;
    return messageModel(message, state.messages.indexOf(message));
  }

  /* Assistant prose where each `[n]` marker is a real button that opens the
   * context panel on that source. A marker with no matching source stays plain
   * text, because `answerSegments` keeps it in the text run. */
  function answerBody(message, value) {
    const node = el("p", "chat-text prose");
    const factory = L();
    const sources = Array.isArray(message.sources) ? message.sources : [];
    if (!factory || typeof factory.answerSegments !== "function") {
      UI().text(node, value);
      return node;
    }
    factory.answerSegments(value, sources.length).forEach((segment) => {
      if (segment.type !== "citation") {
        node.appendChild(el("span", "", segment.text));
        return;
      }
      const button = el("button", "citation-marker", segment.text);
      button.type = "button";
      button.title = "Open source [" + segment.index + "] in the context panel";
      button.addEventListener("click", () => {
        root.AthenaContext?.openSource(String(segment.index), "chat", message.id, button);
      });
      node.appendChild(button);
    });
    return node;
  }

  /* The explicit "Context" control in the chat head: it opens the panel on the
   * newest assistant response and closes it again when it is already open. */
  function toggleContext(trigger) {
    const id = currentMessageId();
    if (!id) {
      UI().toast("Send a message first.");
      return;
    }
    root.AthenaContext?.toggle("sources", "chat", id, trigger || null);
  }

  function currentMessageId() {
    for (let index = state.messages.length - 1; index >= 0; index -= 1) {
      if (state.messages[index].role === "assistant") return state.messages[index].id || null;
    }
    return null;
  }

  function syncContextSurface() {
    if (!root.AthenaContext) return;
    const target = root.AthenaContext.target();
    if (target && target.kind === "chat"
        && state.messages.some((message) => message.id === target.id)) {
      return;
    }
    root.AthenaContext.setSurface("chat", currentMessageId());
  }

  function renderMessage(message, index = -1) {
    if (message.role === "user") {
      const wrap = el("div", "chat-turn user");
      wrap.appendChild(el("span", "turn-label", "You"));
      wrap.appendChild(el("p", "chat-text prose", message.content || ""));
      if (message.capabilities && message.capabilities.length) {
        const badges = el("div", "msg-caps");
        message.capabilities.forEach((cap) => {
          badges.appendChild(el("span", "msg-cap", cap === "rag" ? "Documents" : "Web search"));
        });
        wrap.appendChild(badges);
      }
      return wrap;
    }

    const wrap = el("div", "chat-turn assistant");
    if (message.id) wrap.dataset.messageId = message.id;
    wrap.dataset.contextKind = "chat";
    const head = el("div", "chat-turn-head");
    head.appendChild(el("span", "turn-label", "athena"));
    if (message.provider || message.model) {
      head.appendChild(el("span", "chat-meta", [message.provider, message.model].filter(Boolean).join(" · ")));
    }
    wrap.appendChild(head);

    const trace = message.meta && Array.isArray(message.meta.trace)
      ? message.meta.trace
      : [];
    if (trace.length) {
      wrap.appendChild(traceBlock(message, trace));
    }

    const text = message.content || "";
    if (message.status === "streaming" && !text) {
      const typing = el("span", "chat-typing");
      typing.append(el("i"), el("i"), el("i"));
      wrap.appendChild(typing);
    } else {
      wrap.appendChild(answerBody(message, text));
    }

    if (!trace.length && message.tool_events && message.tool_events.length) {
      wrap.appendChild(toolActivity(message.tool_events));
    }
    const model = messageModel(message, index);
    wrap.appendChild(responseMeta(message, model));
    if (message.sources && message.sources.length) {
      wrap.appendChild(sourcesBlock(message.sources));
    }
    if (message.usage) {
      wrap.appendChild(usageLine(message.usage));
    }
    if (message.error) {
      wrap.appendChild(el("p", "chat-error", message.error));
    }
    if (message.status === "stopped") {
      wrap.appendChild(el("p", "chat-note", "Stopped."));
    }
    const meta = message.meta || {};
    if (meta.tool_note) wrap.appendChild(el("p", "chat-note", meta.tool_note));
    (meta.grounding_notes || []).forEach((note) => wrap.appendChild(el("p", "chat-note", note)));
    return wrap;
  }

  function traceRow(row) {
    const item = el("li", "activity-row is-" + row.status);
    item.appendChild(el("span", "activity-row-dot"));
    const body = el("div", "activity-row-body");
    const head = el("div", "activity-row-head");
    head.appendChild(el("strong", "activity-label", row.label || "Activity"));
    if (row.duration != null) {
      head.appendChild(el("span", "activity-row-time", L().duration(row.duration)));
    }
    body.appendChild(head);
    if (row.detail && row.detail !== row.label) {
      body.appendChild(el("p", "activity-detail", String(row.detail).slice(0, 240)));
    }
    if (row.error) {
      body.appendChild(el("p", "activity-error", String(row.error).slice(0, 240)));
    }
    item.appendChild(body);
    return item;
  }

  function traceBlock(message, trace) {
    const key = message.id || "local-assistant";
    const view = L().chatTrace(trace);
    const live = message.status === "streaming" || view.running;
    const details = document.createElement("details");
    details.className = "activity-trace chat-trace "
      + (live ? "is-live" : "is-complete")
      + (view.failed ? " is-failed" : "");
    details.open = state.traceOpen.has(key)
      || (live && !state.traceClosed.has(key));

    const summary = el("summary", "activity-head");
    summary.appendChild(el("span", "activity-dot " + (view.failed ? "is-error" : live ? "is-live" : "is-done")));
    summary.appendChild(el("span", "activity-state", live ? "Working" : "Activity"));
    summary.appendChild(el("span", "activity-sep", "·"));
    const current = view.current;
    summary.appendChild(el("span", "activity-current", current
      ? current.label + (current.detail && current.detail !== current.label ? " · " + current.detail : "")
      : view.summary));
    if (view.elapsed_ms) {
      summary.appendChild(el("span", "activity-time", L().duration(view.elapsed_ms)));
    }
    const expand = el("span", "activity-expand", details.open ? "Collapse" : "Expand");
    summary.appendChild(expand);
    details.appendChild(summary);

    const list = el("ol", "activity-list");
    view.rows.forEach((row) => list.appendChild(traceRow(row)));
    details.appendChild(list);
    summary.addEventListener("click", () => {
      // Read the browser's post-click state; programmatic `open` changes must
      // never be mistaken for a user's preference.
      requestAnimationFrame(() => {
        if (details.open) {
          state.traceOpen.add(key);
          state.traceClosed.delete(key);
          UI().text(expand, "Collapse");
        } else {
          state.traceOpen.delete(key);
          state.traceClosed.add(key);
          UI().text(expand, "Expand");
        }
      });
    });
    return details;
  }

  function toolActivity(events) {
    const running = events.some((event) => event.status === "running");
    const details = document.createElement("details");
    details.className = "tool-activity";
    if (running) details.open = true;
    details.appendChild(el("summary", "tool-summary", L().toolSummary(events) || "Tools"));
    events.forEach((event) => {
      const row = el("div", "tool-row " + (event.status === "error" ? "is-error" : ""));
      const name = event.status === "running" ? "refresh" : event.status === "error" ? "alert" : "check";
      row.appendChild(icon(name, "icon sm tool-status"));
      const body = el("div", "tool-row-body");
      body.appendChild(el("strong", "tool-name", event.label || event.name || "tool"));
      if (event.summary) body.appendChild(el("span", "tool-result", event.summary));
      if (event.error) body.appendChild(el("span", "tool-error", event.error));
      row.appendChild(body);
      if (event.elapsed_ms) row.appendChild(el("span", "tool-time", Math.round(event.elapsed_ms) + "ms"));
      details.appendChild(row);
    });
    return details;
  }

  function sourcesBlock(sources) {
    const wrap = el("div", "chat-sources");
    wrap.appendChild(el("div", "sidebar-label", "Sources"));
    const chips = el("div", "source-chips");
    sources.forEach((source) => {
      const isWeb = source.kind === "web";
      const chip = document.createElement(isWeb ? "a" : "span");
      chip.className = "source-chip";
      if (isWeb) {
        chip.href = source.url || "#";
        chip.target = "_blank";
        chip.rel = "noopener noreferrer";
      }
      chip.appendChild(icon(isWeb ? "source-web" : "source-doc", "icon sm"));
      chip.appendChild(el("span", "", "[" + source.index + "] " + L().sourceLabel(source)));
      chip.title = source.snippet || source.title || source.document || "";
      chips.appendChild(chip);
    });
    wrap.appendChild(chips);
    const excerpts = sources.filter((source) => source.snippet);
    if (excerpts.length) {
      const details = document.createElement("details");
      details.className = "source-excerpts";
      details.appendChild(el("summary", "", "Source excerpts"));
      excerpts.forEach((source) => {
        const row = el("div", "source-excerpt");
        row.appendChild(el("strong", "", "[" + source.index + "] " + L().sourceLabel(source)));
        row.appendChild(el("p", "", source.snippet));
        details.appendChild(row);
      });
      wrap.appendChild(details);
    }
    return wrap;
  }

  function usageLine(usage) {
    const label = L().usageLabel(usage);
    return el(
      "p",
      "chat-usage",
      [usage.provider, usage.model, label].filter(Boolean).join(" · ")
    );
  }

  function renderHeader() {
    const title = UI().$("chat-title");
    if (title) UI().text(title, (state.conversation && state.conversation.title) || "New chat");
    const meta = UI().$("chat-meta");
    if (meta) {
      const conversation = state.conversation;
      const bits = [];
      if (conversation && conversation.message_count) {
        bits.push(conversation.message_count + " msg" + (conversation.message_count === 1 ? "" : "s"));
      }
      if (conversation && conversation.total_tokens) {
        bits.push(conversation.total_tokens.toLocaleString() + " tok");
      }
      UI().text(meta, bits.join(" · "));
    }
    const select = UI().$("chat-model");
    if (!select) return;
    select.replaceChildren();
    const defaultProfile = state.models.find((profile) => profile.id === state.defaultModel?.profile_id);
    const fallback = [defaultProfile?.name, state.defaultModel?.model].filter(Boolean).join(" · ") || "not configured";
    select.appendChild(new Option("Default · " + fallback, ""));
    state.models.forEach((profile) => {
      const option = new Option(profile.name + " · " + (profile.model || "no model"), profile.id);
      option.disabled = !profile.usable;
      option.title = profile.usable ? "" : profile.reason;
      select.appendChild(option);
    });
    if (state.profileId && !state.models.some((profile) => profile.id === state.profileId)) {
      const unavailable = new Option("Selected model unavailable", state.profileId);
      unavailable.disabled = true;
      select.appendChild(unavailable);
    }
    select.value = state.profileId;
    select.disabled = state.streaming || state.modelSaving || (!state.loaded && Boolean(savedChat.conversationId));
    renderReasoning();
  }

  function supportedReasoning() {
    const model = state.profileId
      ? state.models.find((profile) => profile.id === state.profileId)
      : state.defaultModel;
    return model?.reasoning_options || [];
  }

  function renderReasoning() {
    const trigger = UI().$("chat-reasoning");
    const menu = UI().$("chat-reasoning-menu");
    if (!trigger || !menu) return;
    const supported = supportedReasoning();
    const value = supported.includes(state.reasoningEffort) ? state.reasoningEffort : "";
    for (const option of menu.children) {
      const choice = option.dataset.effort;
      const selected = choice === value;
      option.setAttribute("aria-disabled", String(choice !== "" && !supported.includes(choice)));
      option.setAttribute("aria-selected", String(selected));
      if (selected) UI().text(UI().$("chat-reasoning-current"), option.firstElementChild.textContent);
    }
    trigger.title = supported.length ? "Reasoning effort for Chat" : "Only model default is available for this model.";
    trigger.disabled = state.streaming || state.modelSaving || (!state.loaded && Boolean(savedChat.conversationId));
    if (trigger.disabled) closeReasoningMenu();
  }

  function closeReasoningMenu(restoreFocus = false) {
    const menu = UI().$("chat-reasoning-menu");
    if (!menu || menu.classList.contains("hidden")) return;
    const trigger = UI().$("chat-reasoning");
    menu.classList.add("hidden");
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus({ preventScroll: true });
  }

  function wireReasoning() {
    const picker = UI().$("chat-reasoning-picker");
    if (!picker) return;
    const trigger = UI().$("chat-reasoning");
    const menu = UI().$("chat-reasoning-menu");
    for (const effort of ["", ...REASONING_OPTIONS]) {
      const option = el("li", "pattern-option");
      option.dataset.effort = effort;
      option.setAttribute("role", "option");
      option.tabIndex = -1;
      const label = effort ? effort[0].toUpperCase() + effort.slice(1) : "Model default";
      option.append(el("span", "", label), icon("check"));
      menu.appendChild(option);
    }

    const open = () => {
      if (trigger.disabled) return;
      menu.classList.remove("hidden");
      trigger.setAttribute("aria-expanded", "true");
      menu.querySelector('[aria-selected="true"]')?.focus();
    };
    const choose = (option) => {
      if (!option || option.getAttribute("aria-disabled") === "true") return;
      state.reasoningEffort = option.dataset.effort;
      rememberChat();
      renderReasoning();
      closeReasoningMenu(true);
    };
    trigger.addEventListener("click", () => {
      if (menu.classList.contains("hidden")) open();
      else closeReasoningMenu();
    });
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "Escape") return closeReasoningMenu();
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      event.preventDefault();
      open();
    });
    menu.addEventListener("click", (event) => choose(event.target.closest("[data-effort]")));
    menu.addEventListener("keydown", (event) => {
      if (event.key === "Tab") return closeReasoningMenu(true);
      const keys = ["ArrowDown", "ArrowUp", "Home", "End", "Enter", " ", "Escape"];
      if (!keys.includes(event.key)) return;
      event.preventDefault();
      if (event.key === "Escape") return closeReasoningMenu(true);
      if (event.key === "Enter" || event.key === " ") return choose(event.target);

      const options = [...menu.querySelectorAll('[aria-disabled="false"]')];
      const index = options.indexOf(event.target);
      if (event.key === "Home") options[0]?.focus();
      else if (event.key === "End") options.at(-1)?.focus();
      else {
        const step = event.key === "ArrowDown" ? 1 : -1;
        const next = index < 0
          ? (step > 0 ? 0 : options.length - 1)
          : (index + step + options.length) % options.length;
        options[next]?.focus();
      }
    });
    picker.addEventListener("focusout", (event) => {
      if (!picker.contains(event.relatedTarget)) closeReasoningMenu();
    });
    document.addEventListener("click", (event) => {
      if (!picker.contains(event.target)) closeReasoningMenu();
    }, true);
    renderReasoning();
  }

  function renderEmptyHint() {
    const hint = UI().$("chat-empty-hint");
    if (!hint) return;
    const configured = state.profileId
      ? state.models.some((profile) => profile.id === state.profileId && profile.usable)
      : state.defaultModel?.configured;
    hint.replaceChildren();
    if (configured) return;
    if (state.profileId) {
      UI().text(hint, "The selected model is unavailable. Choose another model or update the profile.");
    } else {
      UI().text(hint, "No provider is configured. Add a model in Settings to start chatting.");
    }
    const button = el("button", "link-button", "Open Settings");
    button.type = "button";
    button.addEventListener("click", () => root.AthenaSettings?.open(state.profileId ? "models" : "add-model"));
    hint.appendChild(document.createTextNode(" "));
    hint.appendChild(button);
  }

  function scrollToBottom() {
    const main = UI().$("main");
    if (!main) return;
    const nearBottom = main.scrollHeight - main.scrollTop - main.clientHeight < 160;
    if (nearBottom) main.scrollTop = main.scrollHeight;
  }

  /* ---------------------------------------------------------- conversation */

  async function refreshList() {
    const request = ++state.listRequest;
    try {
      const data = await UI().api("/api/chat/conversations?limit=12", { cache: "no-store" });
      if (request !== state.listRequest) return;
      const host = UI().$("chat-list");
      if (!host) return;
      host.replaceChildren();
      const rows = data.conversations || [];
      if (!rows.length) {
        host.appendChild(el("li", "sidebar-empty", "no chats yet"));
        return;
      }
      rows.forEach((conversation) => {
        const item = document.createElement("li");
        item.className = "chat-row history-entry" + (conversation.id === state.conversationId ? " is-active" : "");
        const button = el("button", "chat-row-btn");
        button.type = "button";
        const title = conversation.title || "New chat";
        button.appendChild(el("span", "chat-row-title", title));
        const meta = [relative(conversation.updated_at), conversation.total_tokens ? conversation.total_tokens.toLocaleString() + " tok" : ""]
          .filter(Boolean).join(" · ");
        if (meta) button.appendChild(el("span", "chat-row-meta", meta));
        button.addEventListener("click", () => openConversation(conversation.id));
        const remove = el("button", "history-row-delete");
        remove.type = "button";
        remove.title = "Delete chat";
        remove.setAttribute("aria-label", "Delete chat: " + title);
        remove.appendChild(icon("delete"));
        remove.addEventListener("click", () => deleteConversation(conversation.id, remove, title));
        item.append(button, remove);
        host.appendChild(item);
      });
    } catch (_) {
      if (request !== state.listRequest) return;
      const host = UI().$("chat-list");
      if (host && !host.childElementCount) host.appendChild(el("li", "sidebar-empty", "chats unavailable"));
    }
  }

  async function openConversation(id, restoring = false) {
    if (state.streaming) { UI().toast("Stop the current reply first."); return; }
    saveCurrentScroll();
    const token = state.navToken + 1;
    state.navToken = token;
    if (root.AthenaContext) root.AthenaContext.setSurface("chat", null);
    try {
      const data = await UI().api("/api/chat/conversations/" + encodeURIComponent(id), { cache: "no-store" });
      if (token !== state.navToken) return;
      state.conversationId = id;
      state.conversation = data.conversation;
      state.messages = data.messages || [];
      state.profileId = data.conversation.profile_id || "";
      rememberChat();
      state.restoreScroll = Number.isFinite(state.scrollMap[id]) ? state.scrollMap[id] : null;
      state.traceOpen.clear();
      state.traceClosed.clear();
      renderHeader();
      renderTranscript();
      renderEmptyHint();
      syncContextSurface();
      await Promise.all([!restoring && refreshList(), loadCapabilities(), refreshSelectedTools()]);
      return true;
    } catch (error) {
      if (token !== state.navToken) return;
      root.AthenaContext?.setSurface("chat", currentMessageId());
      if (restoring && error.status === 404) {
        rememberChat();
        return false;
      }
      UI().toast("Could not open chat: " + error.message);
    }
  }

  function newConversation() {
    if (state.streaming) { UI().toast("Stop the current reply first."); return; }
    saveCurrentScroll();
    state.navToken += 1;
    state.conversationId = null;
    state.conversation = null;
    state.messages = [];
    state.profileId = "";
    rememberChat();
    state.streamEls = null;
    state.restoreScroll = null;
    state.traceOpen.clear();
    state.traceClosed.clear();
    UI().$("chat-error").classList.add("hidden");
    UI().text(UI().$("chat-error"), "");
    renderHeader();
    renderTranscript();
    renderEmptyHint();
    syncContextSurface();
    if (root.AthenaChat) refreshList();
    loadCapabilities();
    refreshSelectedTools();
    const input = UI().$("chat-input");
    if (input) { input.value = ""; input.focus(); }
  }

  /* ------------------------------------------------------------- grounding */

  function syncCapabilities() {
    ["rag", "web_search"].forEach((cap) => {
      const id = cap === "rag" ? "cap-rag" : "cap-web";
      const button = UI().$(id);
      if (!button) return;
      const available = button.dataset.available !== "0";
      const on = state.capabilities.has(cap) && available;
      button.classList.toggle("is-on", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
      button.disabled = !available;
    });
  }

  async function loadCapabilities() {
    const request = ++state.capabilityRequest;
    try {
      const query = state.profileId ? "?profile_id=" + encodeURIComponent(state.profileId) : "";
      const data = await UI().api("/api/chat/capabilities" + query);
      if (request !== state.capabilityRequest) return;
      (data.capabilities || []).forEach((cap) => {
        const id = cap.id === "rag" ? "cap-rag" : "cap-web";
        const button = UI().$(id);
        if (!button) return;
        button.dataset.available = cap.available ? "1" : "0";
        button.title = cap.available ? cap.description : cap.reason;
        if (!cap.available) state.capabilities.delete(cap.id);
      });
      saveList(CAPS_KEY, state.capabilities);
      syncCapabilities();
    } catch (_) {
      if (request !== state.capabilityRequest) return;
      ["cap-rag", "cap-web"].forEach((id) => {
        const button = UI().$(id);
        if (button) { button.dataset.available = "0"; button.title = "Capability status unavailable."; }
      });
      syncCapabilities();
    }
  }

  function toggleCapability(cap) {
    if (state.capabilities.has(cap)) state.capabilities.delete(cap);
    else state.capabilities.add(cap);
    saveList(CAPS_KEY, state.capabilities);
    syncCapabilities();
  }

  /* ------------------------------------------------------------------ tools */

  function syncTools() {
    const button = UI().$("cap-tools");
    if (!button) return;
    const count = state.tools.size;
    button.classList.toggle("is-on", count > 0);
    button.setAttribute("aria-pressed", count > 0 ? "true" : "false");
    button.title = count ? count + " tool(s) selected" : "Choose tools for the next message";
    const status = UI().$("chat-toolbar-status");
    if (status) UI().text(status, count ? count + " tool" + (count === 1 ? "" : "s") : "");
  }

  function renderToolPicker() {
    const host = UI().$("tools-popover-list");
    if (!host) return;
    host.replaceChildren();
    if (!state.toolCatalog.length) {
      host.appendChild(el("p", "empty-state", "No tools available."));
      return;
    }
    state.toolCatalog.forEach((tool) => {
      const label = document.createElement("label");
      label.className = "tool-pick" + (tool.available ? "" : " is-disabled");
      label.title = tool.available ? tool.description : tool.reason;
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = state.tools.has(tool.id) && tool.available;
      box.disabled = !tool.available;
      box.addEventListener("change", () => {
        if (box.checked) state.tools.add(tool.id);
        else state.tools.delete(tool.id);
        saveList(TOOLS_KEY, state.tools);
        syncTools();
      });
      label.appendChild(box);
      const body = el("span", "tool-pick-body");
      body.appendChild(el("strong", "", tool.label || tool.id));
      body.appendChild(el("span", "tool-pick-desc", tool.description || ""));
      if (!tool.available && tool.reason) body.appendChild(el("span", "tool-pick-reason", tool.reason));
      label.appendChild(body);
      host.appendChild(label);
    });
  }

  async function loadTools() {
    const request = ++state.toolRequest;
    const host = UI().$("tools-popover-list");
    if (host) host.replaceChildren(el("p", "empty-state", "Loading tools…"));
    try {
      const query = state.profileId ? "?profile_id=" + encodeURIComponent(state.profileId) : "";
      const data = await UI().api("/api/chat/tools" + query);
      if (request !== state.toolRequest) return;
      state.toolCatalog = data.tools || [];
      const available = new Set(state.toolCatalog.filter(isAvailable).map((tool) => tool.id));
      state.tools.forEach((name) => { if (!available.has(name)) state.tools.delete(name); });
      saveList(TOOLS_KEY, state.tools);
      renderToolPicker();
      syncTools();
    } catch (_) {
      if (request !== state.toolRequest) return;
      state.toolCatalog = [];
      renderToolPicker();
      syncTools();
    }
  }

  function refreshSelectedTools() {
    const picker = UI().$("tools-popover");
    if (state.tools.size || (picker && !picker.classList.contains("hidden"))) return loadTools();
    state.toolRequest += 1;
  }

  /* ------------------------------------------------------------------ models */

  async function loadModels({ resetDraft = false } = {}) {
    if (resetDraft && !state.conversationId && (state.loaded || !savedChat.conversationId)) {
      state.profileId = "";
      rememberChat();
    }
    const request = ++state.modelRequest;
    try {
      const data = await UI().api("/api/chat/models", { cache: "no-store" });
      if (request !== state.modelRequest) return;
      state.models = data.profiles || [];
      state.defaultModel = data.default || null;
      if (!state.conversationId && state.profileId
          && !state.models.some((profile) => profile.id === state.profileId && profile.usable)) {
        state.profileId = "";
        rememberChat();
      }
      renderHeader();
      renderEmptyHint();
      if (resetDraft && state.loaded) await Promise.all([loadCapabilities(), refreshSelectedTools()]);
    } catch (error) {
      if (request !== state.modelRequest) return;
      UI().toast("Could not load models: " + error.message);
    }
  }

  async function changeModel(profileId) {
    const previous = state.profileId;
    const conversationId = state.conversationId;
    state.profileId = profileId || "";
    if (conversationId) {
      const token = state.navToken;
      state.modelSaving = true;
      renderHeader();
      try {
        const data = await UI().api("/api/chat/conversations/" + encodeURIComponent(conversationId) + "/model", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile_id: state.profileId }),
          keepalive: true,
        });
        if (token !== state.navToken) return;
        state.conversation = data.conversation;
      } catch (error) {
        if (token !== state.navToken) return;
        state.profileId = previous;
        UI().toast("Could not change the model: " + error.message);
      } finally {
        state.modelSaving = false;
        renderHeader();
      }
    } else {
      rememberChat();
    }
    renderReasoning();
    renderEmptyHint();
    await Promise.all([loadCapabilities(), refreshSelectedTools()]);
  }

  /* ------------------------------------------------------------------- send */

  function setStreaming(on) {
    state.streaming = on;
    const input = UI().$("chat-input");
    const send = UI().$("chat-send");
    const stop = UI().$("chat-stop");
    const select = UI().$("chat-model");
    if (input) input.disabled = on;
    if (send) send.hidden = on;
    if (stop) stop.hidden = !on;
    if (select) select.disabled = on || state.modelSaving;
    renderReasoning();
    const status = UI().$("chat-status");
    if (status) {
      status.classList.toggle("hidden", !on);
      UI().text(status, on ? "athena is thinking…" : "");
    }
  }

  function upsertToolEvent(assistant, payload) {
    const rows = assistant.tool_events || (assistant.tool_events = []);
    const index = rows.findIndex((row) => row.id === payload.id);
    if (index >= 0) rows[index] = payload;
    else rows.push(payload);
  }

  let streamFrame = 0;
  let streamDetailsChanged = false;

  function queueStreamRender(assistant, detailsChanged = false) {
    if (UI().currentView() !== "chat") return;
    streamDetailsChanged ||= detailsChanged;
    if (streamFrame) return;
    streamFrame = requestAnimationFrame(() => {
      streamFrame = 0;
      const refreshDetails = streamDetailsChanged;
      streamDetailsChanged = false;
      if (state.messages.at(-1) !== assistant || UI().currentView() !== "chat") return;
      const main = UI().$("main");
      const follow = UI().currentView() === "chat" && main
        && main.scrollHeight - main.scrollTop - main.clientHeight < 160;
      const node = state.streamEls?.text;
      if (refreshDetails || !node || node.classList.contains("chat-typing")) {
        const host = UI().$("chat-transcript");
        const reply = renderMessage(assistant, state.messages.length - 1);
        host.lastElementChild?.replaceWith(reply);
        state.streamEls = { text: reply.querySelector(".chat-text, .chat-typing") };
      } else {
        node.textContent = assistant.content;
      }
      if (follow) main.scrollTop = main.scrollHeight;
      if (refreshDetails) root.AthenaContext?.refresh("chat", assistant.id);
    });
  }

  function applyEvent(assistant, name, payload) {
    if (name === "meta") {
      if (payload.conversation) {
        state.conversationId = payload.conversation.id;
        state.conversation = payload.conversation;
        rememberChat();
        renderHeader();
      }
      if (payload.user_message) {
        const user = state.messages.find((message) => message.id === "local-user");
        if (user) Object.assign(user, payload.user_message);
      }
      if (payload.assistant_message) {
        const previousId = assistant.id;
        Object.assign(assistant, payload.assistant_message);
        assistant.content = "";
        assistant.tool_events = [];
        assistant.sources = [];
        if (root.AthenaContext) {
          const target = root.AthenaContext.target();
          if (target && target.kind === "chat" && target.id === previousId) {
            root.AthenaContext.setSurface("chat", assistant.id);
          }
        }
      }
      queueStreamRender(assistant, true);
      return;
    }
    if (name === "trace") {
      assistant.meta = assistant.meta || {};
      const trace = assistant.meta.trace || (assistant.meta.trace = []);
      const index = trace.findIndex((event) => event.id === payload.id);
      if (index >= 0) trace[index] = payload;
      else trace.push(payload);
      queueStreamRender(assistant, true);
      return;
    }
    if (name === "sources") {
      assistant.sources = payload.sources || [];
      queueStreamRender(assistant, true);
      return;
    }
    if (name === "tool") {
      upsertToolEvent(assistant, payload);
      queueStreamRender(assistant, true);
      return;
    }
    if (name === "usage") {
      assistant.usage = payload;
      queueStreamRender(assistant, true);
      return;
    }
    if (name === "note") {
      assistant.meta = assistant.meta || {};
      const notes = assistant.meta.grounding_notes || (assistant.meta.grounding_notes = []);
      if (payload.text && !notes.includes(payload.text)) notes.push(payload.text);
      queueStreamRender(assistant, true);
      return;
    }
    if (name === "error") {
      assistant.error = payload.detail || "The reply failed.";
      queueStreamRender(assistant, true);
      return;
    }
    if (name === "delta") {
      assistant.content = (assistant.content || "") + (payload.text || "");
      queueStreamRender(assistant);
    }
  }

  async function readStream(response, assistant) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    const parser = L().sseParser();
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      const frames = parser.push(decoder.decode(value, { stream: true }));
      frames.forEach((frame) => {
        let payload = {};
        try { payload = JSON.parse(frame.data); } catch (_) { payload = {}; }
        if (frame.event === "done") {
          if (payload.message) Object.assign(assistant, payload.message);
          if (payload.conversation) {
            state.conversation = payload.conversation;
            state.conversationId = payload.conversation.id;
          }
          state.streaming = false;
          root.AthenaContext?.refresh("chat", assistant.id);
          return;
        }
        applyEvent(assistant, frame.event, payload);
      });
      if (!state.streaming && assistant.status && assistant.status !== "streaming") break;
    }
    parser.flush().forEach((frame) => {
      try { applyEvent(assistant, frame.event, JSON.parse(frame.data)); } catch (_) { /* ignore */ }
    });
  }

  async function send() {
    if (state.streaming) return;
    if (state.modelSaving || (!state.loaded && savedChat.conversationId)) {
      UI().toast("Wait for the chat model to finish loading.");
      return;
    }
    state.navToken += 1;
    const input = UI().$("chat-input");
    const message = input ? input.value.trim() : "";
    if (!message) return;
    const errorNode = UI().$("chat-error");
    if (errorNode) { errorNode.classList.add("hidden"); UI().text(errorNode, ""); }
    input.value = "";
    input.style.height = "auto";

    const user = {
      id: "local-user", role: "user", content: message, status: "ok",
      capabilities: [...state.capabilities],
    };
    const assistant = {
      id: "local-assistant", role: "assistant", content: "", status: "streaming",
      provider: "", model: "", capabilities: [...state.capabilities],
      tools: [...state.tools], sources: [], tool_events: [], usage: null, meta: {}, error: "",
    };
    state.messages.push(user, assistant);
    renderTranscript();
    setStreaming(true);
    const status = UI().$("chat-status");
    if (status) UI().text(status, "athena is thinking…");

    const controller = new AbortController();
    state.controller = controller;
    const body = {
      message,
      capabilities: [...state.capabilities],
      tools: [...state.tools],
    };
    if (state.conversationId) body.conversation_id = state.conversationId;
    if (state.profileId) body.profile_id = state.profileId;
    if (supportedReasoning().includes(state.reasoningEffort)) body.reasoning_effort = state.reasoningEffort;

    try {
      const response = await fetch("/api/chat/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!response.ok) {
        let detail = "HTTP " + response.status;
        try {
          const parsed = await response.json();
          detail = parsed.detail || detail;
        } catch (_) { /* keep the status */ }
        throw new Error(detail);
      }
      await readStream(response, assistant);
    } catch (error) {
      if (error && error.name === "AbortError") {
        assistant.status = "stopped";
        assistant.error = "";
        if (state.conversationId) {
          try {
            await fetch("/api/chat/conversations/" + encodeURIComponent(state.conversationId) + "/stop", {
              method: "POST",
            });
          } catch (_) { /* best effort */ }
        }
      } else {
        assistant.status = "error";
        assistant.error = error.message || "Chat failed.";
        if (errorNode) {
          errorNode.classList.remove("hidden");
          UI().text(errorNode, assistant.error);
        }
      }
    } finally {
      if (streamFrame) cancelAnimationFrame(streamFrame);
      streamFrame = 0;
      streamDetailsChanged = false;
      state.streaming = false;
      state.controller = null;
      setStreaming(false);
      if (assistant.status === "streaming") assistant.status = "ok";
      renderHeader();
      renderTranscript();
      root.AthenaContext?.refresh("chat", assistant.id);
      refreshList();
      if (input) input.focus();
    }
  }

  function stop() {
    if (state.controller) state.controller.abort();
  }

  /* ----------------------------------------------------------------- header */

  function toggleMenu(force) {
    const menu = UI().$("chat-menu");
    const button = UI().$("chat-title-btn");
    if (!menu || !button) return;
    const open = force === undefined ? menu.classList.contains("hidden") : force;
    menu.classList.toggle("hidden", !open);
    button.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function renameInline() {
    const title = UI().$("chat-title");
    if (!title) return;
    const original = title.textContent || "";
    const input = document.createElement("input");
    input.type = "text";
    input.className = "chat-title-input";
    input.maxLength = 200;
    input.value = original;
    title.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const restore = (value) => {
      const span = el("span", "", value);
      span.id = "chat-title";
      input.replaceWith(span);
    };
    const commit = async () => {
      if (done) return;
      done = true;
      const value = input.value.trim();
      if (!value || value === original) { restore(original); return; }
      if (!state.conversationId) { restore(value); return; }
      try {
        const data = await UI().api("/api/chat/conversations/" + encodeURIComponent(state.conversationId), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: value }),
        });
        state.conversation = data.conversation;
        restore(data.conversation.title);
        renderHeader();
        refreshList();
      } catch (error) {
        restore(original);
        UI().toast("Rename failed: " + error.message);
      }
    };
    input.addEventListener("blur", commit);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); input.blur(); }
      if (event.key === "Escape") { event.preventDefault(); done = true; restore(original); }
    });
  }

  async function copyTranscript() {
    if (!state.conversationId) { UI().toast("Nothing to copy yet."); return; }
    try {
      const response = await fetch("/api/chat/conversations/" + encodeURIComponent(state.conversationId) + "/transcript");
      if (!response.ok) throw new Error("HTTP " + response.status);
      await navigator.clipboard.writeText(await response.text());
      UI().toast("Conversation copied.");
    } catch (error) {
      UI().toast("Copy failed: " + error.message);
    }
  }

  async function deleteConversation(id = state.conversationId, button, title = state.conversation?.title || "New chat") {
    if (!id) { UI().toast("No chat to delete."); return; }
    if (id === state.conversationId && state.streaming) { UI().toast("Stop the current reply first."); return; }
    if (!await UI().confirmDelete({
      title: "Delete this chat?",
      target: title,
      detail: "Its messages will be permanently deleted.",
      trigger: button || UI().$("chat-title-btn"),
    })) return;
    if (id === state.conversationId && state.streaming) { UI().toast("Stop the current reply first."); return; }
    if (button) button.disabled = true;
    try {
      await UI().api("/api/chat/conversations/" + encodeURIComponent(id), { method: "DELETE" });
      button?.closest("li")?.remove();
      if (id === state.conversationId) newConversation();
      else refreshList();
      delete state.scrollMap[id];
      saveScrollMap();
      UI().toast("Chat deleted.");
    } catch (error) {
      UI().toast("Delete failed: " + error.message);
    } finally {
      if (button) button.disabled = false;
    }
  }

  /* ------------------------------------------------------------------- init */

  async function activate() {
    state.active = true;
    if (!state.loaded) {
      state.loading ||= (async () => {
        const models = loadModels();
        const list = refreshList();
        const profileId = state.profileId;
        if (savedChat.conversationId && state.navToken === 0) {
          const [, restored] = await Promise.all([models, openConversation(savedChat.conversationId, true)]);
          if (restored) {
            await list;
            return;
          }
        }
        const loadGrounding = () => Promise.all([loadCapabilities(), refreshSelectedTools()]);
        await Promise.all([list, profileId ? models.then(loadGrounding) : loadGrounding(), models]);
        renderEmptyHint();
        renderTranscript();
        syncContextSurface();
      })().finally(() => {
        state.loaded = true;
        state.loading = null;
        renderHeader();
      });
      return state.loading;
    }
    await refreshList();
    renderHeader();
    renderTranscript();
    syncContextSurface();
  }

  function autoGrow() {
    const input = UI().$("chat-input");
    if (!input) return;
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  }

  function wire() {
    if (root.AthenaContext) root.AthenaContext.register("chat", contextModel);
    state.scrollMap = loadScrollMap();
    state.empty = UI().$("chat-empty");
    const main = UI().$("main");
    if (main) {
      main.addEventListener("scroll", () => {
        if (state.scrollTimer) clearTimeout(state.scrollTimer);
        state.scrollTimer = setTimeout(() => {
          state.scrollTimer = null;
          if (UI().currentView() === "chat") saveCurrentScroll();
        }, 150);
      }, { passive: true });
    }
    window.addEventListener("beforeunload", saveCurrentScroll);
    document.addEventListener("athena:view", (event) => {
      const view = event.detail && event.detail.view;
      if (view === "chat") syncContextSurface();
      else saveCurrentScroll();
    });
    const form = UI().$("chat-form");
    if (form) form.addEventListener("submit", (event) => { event.preventDefault(); send(); });
    const input = UI().$("chat-input");
    if (input) {
      input.addEventListener("input", autoGrow);
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          send();
        }
      });
    }
    const stopButton = UI().$("chat-stop");
    if (stopButton) stopButton.addEventListener("click", stop);
    ["cap-rag", "cap-web"].forEach((id) => {
      const button = UI().$(id);
      if (button) button.addEventListener("click", () => toggleCapability(id === "cap-rag" ? "rag" : "web_search"));
    });
    const toolsButton = UI().$("cap-tools");
    const popover = UI().$("tools-popover");
    if (toolsButton && popover) {
      toolsButton.addEventListener("click", (event) => {
        event.stopPropagation();
        const hidden = popover.classList.toggle("hidden");
        toolsButton.setAttribute("aria-expanded", hidden ? "false" : "true");
        if (!hidden) loadTools();
      });
      popover.addEventListener("click", (event) => event.stopPropagation());
    }
    const closeTools = UI().$("tools-popover-close");
    if (closeTools && popover) {
      closeTools.addEventListener("click", () => {
        popover.classList.add("hidden");
        toolsButton.setAttribute("aria-expanded", "false");
      });
    }
    const model = UI().$("chat-model");
    if (model) model.addEventListener("change", () => changeModel(model.value));
    wireReasoning();
    syncTools();
    const contextToggle = UI().$("context-toggle");
    if (contextToggle) {
      contextToggle.addEventListener("click", (event) => {
        toggleContext(event.currentTarget);
      });
    }
    const refresh = UI().$("chat-refresh");
    if (refresh) refresh.addEventListener("click", refreshList);
    const title = UI().$("chat-title-btn");
    if (title) title.addEventListener("click", (event) => { event.stopPropagation(); toggleMenu(); });
    const menu = UI().$("chat-menu");
    if (menu) {
      menu.addEventListener("click", (event) => {
        const button = event.target.closest("button[data-action]");
        if (!button) return;
        toggleMenu(false);
        if (button.dataset.action === "rename") renameInline();
        if (button.dataset.action === "copy") copyTranscript();
        if (button.dataset.action === "delete") deleteConversation();
      });
      menu.addEventListener("click", (event) => event.stopPropagation());
    }
    document.addEventListener("click", () => {
      toggleMenu(false);
      if (popover && !popover.classList.contains("hidden")) {
        popover.classList.add("hidden");
        if (toolsButton) toolsButton.setAttribute("aria-expanded", "false");
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        toggleMenu(false);
        if (popover) {
          popover.classList.add("hidden");
          if (toolsButton) toolsButton.setAttribute("aria-expanded", "false");
        }
      }
    });
    if (UI().currentView && UI().currentView() === "chat") activate();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();

  root.AthenaChat = {
    activate,
    reloadCapabilities: loadCapabilities,
    newConversation,
    openConversation,
    refresh: refreshList,
    reloadModels: loadModels,
    stop,
    isStreaming: () => state.streaming,
    currentMessageId,
    state: () => state,
  };
}(typeof globalThis !== "undefined" ? globalThis : this));
