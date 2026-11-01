/* First-run feature tour. It highlights real controls and never blocks the app. */
(function (root) {
  "use strict";

  const UI = () => root.AthenaUi;
  const L = () => root.AthenaLogic;
  const STORAGE_KEY = "athena.tutorial.v1";

  const STEPS = [
    {
      id: "welcome",
      title: "Welcome to athena",
      body: "athena has two modes. Chat is an ordinary conversation with the model you choose. Research is a separate multi-agent workflow with plans, evidence, a critic and run history.",
    },
    {
      id: "chat",
      title: "Chat",
      body: "Start a new chat here. Previous conversations stay in the Chats list, and reopening one restores its transcript.",
      target: "#nav-chat",
      placement: "top",
      before: () => {
        root.AthenaSettings?.close();
        UI().showView("chat");
        if (window.innerWidth > 680 && document.body.classList.contains("sidebar-collapsed")) {
          document.querySelector("#sidebar-toggle")?.click();
        }
      },
    },
    {
      id: "model",
      title: "Choose the model",
      body: "Pick a configured provider profile. Chat uses your selection; Research keeps using the active profile in Settings → Models.",
      target: "#chat-model",
      placement: "bottom",
      before: () => { root.AthenaSettings?.close(); UI().showView("chat"); },
    },
    {
      id: "grounding",
      title: "Grounding is opt-in",
      body: "Documents and Web search are explicit capabilities. When one is enabled, athena shows the real sources. If the index is empty or search is disabled, the control says why.",
      target: "#chat-capabilities",
      placement: "top",
      before: () => { root.AthenaSettings?.close(); UI().showView("chat"); },
    },
    {
      id: "tools",
      title: "Selected tools only",
      body: "Choose native tools for the message. The model can call only those tools, in bounded rounds, and every call appears in the transcript with its status.",
      target: "#cap-tools",
      placement: "top",
      before: () => { root.AthenaSettings?.close(); UI().showView("chat"); },
    },
    {
      id: "research",
      title: "Research is separate",
      body: "Research runs the existing MAS pipeline: a planner, researchers, a writer and a critic. Use the pattern selector and inspect activity, the blackboard, metrics and run history.",
      target: "#nav-run",
      placement: "top",
      before: () => {
        root.AthenaSettings?.close();
        UI().showView("run");
        if (window.innerWidth > 680 && document.body.classList.contains("sidebar-collapsed")) {
          document.querySelector("#sidebar-toggle")?.click();
        }
      },
    },
    {
      id: "library",
      title: "Library",
      body: "Library shows the real RAG index, the corpus, a rebuild control and recent research. Saved chat documents are not part of MAS yet.",
      target: '#settings-nav [data-section="library"]',
      placement: "right",
      before: () => root.AthenaSettings?.open("library"),
    },
    {
      id: "settings",
      title: "Settings",
      body: "Providers, chat defaults, appearance, personalisation and the read-only built-in MCP status all live here. You can restart this tour from About at any time.",
      target: "#settings-nav",
      placement: "right",
      before: () => root.AthenaSettings?.open("about"),
    },
  ];

  const state = {
    open: false,
    index: 0,
    token: 0,
    lastFocus: null,
    skipped: new Set(),
    onScroll: null,
  };

  function readState() {
    try { return L().parseTutorialState(localStorage.getItem(STORAGE_KEY)); } catch (_) { return null; }
  }

  function persist(status, index) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        status: status,
        step: index,
        updated_at: new Date().toISOString(),
      }));
    } catch (_) { /* private mode: the in-memory run still works */ }
  }

  function busy() {
    return Boolean(UI() && UI().isBusy && UI().isBusy())
      || Boolean(root.AthenaChat && root.AthenaChat.isStreaming && root.AthenaChat.isStreaming());
  }

  function waitFor(selector, timeout) {
    return new Promise((resolve) => {
      const started = performance.now();
      const tick = () => {
        const node = document.querySelector(selector);
        if (node) {
          const rect = node.getBoundingClientRect();
          const inViewport = rect.width > 0 && rect.height > 0
            && rect.bottom > 0 && rect.right > 0
            && rect.top < window.innerHeight && rect.left < window.innerWidth;
          if (inViewport) { resolve(node); return; }
        }
        if (performance.now() - started > (timeout || 800)) { resolve(null); return; }
        window.requestAnimationFrame(tick);
      };
      tick();
    });
  }

  function cardNodes() {
    return Array.from(document.querySelectorAll("#tutorial-card button"))
      .filter((node) => !node.disabled && node.offsetParent !== null);
  }

  function trap(event) {
    const nodes = cardNodes();
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

  function hideSpotlight() {
    const spotlight = UI().$("tutorial-spotlight");
    if (spotlight) spotlight.classList.add("hidden");
    const card = UI().$("tutorial-card");
    if (card) card.classList.add("is-centered");
  }

  function place(target) {
    const card = UI().$("tutorial-card");
    const spotlight = UI().$("tutorial-spotlight");
    if (!card || !spotlight) return;
    if (!target) {
      hideSpotlight();
      card.style.left = "";
      card.style.top = "";
      return;
    }
    card.classList.remove("is-centered");
    const rect = target.getBoundingClientRect();
    spotlight.classList.remove("hidden");
    spotlight.style.left = (rect.left - 6) + "px";
    spotlight.style.top = (rect.top - 6) + "px";
    spotlight.style.width = (rect.width + 12) + "px";
    spotlight.style.height = (rect.height + 12) + "px";
    const cardRect = card.getBoundingClientRect();
    const placed = L().tutorialPlacement(
      rect,
      { width: cardRect.width || 340, height: cardRect.height || 220 },
      { width: window.innerWidth, height: window.innerHeight },
      STEPS[state.index] && STEPS[state.index].placement
    );
    card.classList.toggle("is-mobile", Boolean(placed.mobile));
    card.dataset.placement = placed.placement;
    if (placed.mobile) {
      card.style.left = "";
      card.style.top = "";
    } else {
      card.style.left = placed.left + "px";
      card.style.top = placed.top + "px";
    }
  }

  function updateChrome() {
    const step = STEPS[state.index];
    if (!step) return;
    UI().text(UI().$("tutorial-progress"), L().tutorialProgress(state.index, STEPS.length));
    UI().text(UI().$("tutorial-title"), step.title);
    UI().text(UI().$("tutorial-body"), step.body);
    const back = UI().$("tutorial-back");
    if (back) back.disabled = state.index === 0;
    const next = UI().$("tutorial-next");
    if (next) UI().text(next.querySelector("span") || next, state.index === STEPS.length - 1 ? "Finish" : "Next");
  }

  async function go(index) {
    if (!state.open) return;
    if (index < 0) index = 0;
    if (index >= STEPS.length) { finish(); return; }
    const previous = state.index;
    const token = ++state.token;
    state.index = index;
    const step = STEPS[index];
    updateChrome();
    const card = UI().$("tutorial-card");
    if (card) card.focus();
    try {
      if (step.before) await step.before();
      const target = step.target ? await waitFor(step.target) : null;
      if (token !== state.token) return;
      if (step.target && !target) {
        state.skipped.add(step.id);
        if (state.skipped.size >= STEPS.length) {
          close("skipped");
          UI().toast("The tour could not find its targets on this layout.");
          return;
        }
        // Keep moving in the direction the user asked for, so Back never
        // bounces on a step whose target is hidden by the layout.
        go(index + (index >= previous ? 1 : -1));
        return;
      }
      state.skipped.delete(step.id);
      if (target) {
        target.scrollIntoView({
          block: "center",
          inline: "nearest",
          behavior: "auto",
        });
        await new Promise((resolve) => window.requestAnimationFrame(() => window.requestAnimationFrame(resolve)));
        if (token !== state.token) return;
      }
      place(target);
      const next = UI().$("tutorial-next");
      if (next) next.focus();
    } catch (_) {
      if (token !== state.token) return;
      state.skipped.add(step.id);
      go(index + (index >= previous ? 1 : -1));
    }
  }

  function next() {
    if (state.index >= STEPS.length - 1) finish();
    else go(state.index + 1);
  }

  function back() {
    if (state.index > 0) go(state.index - 1);
  }

  function finish() {
    persist("done", state.index);
    close(null);
    UI().toast("Tour complete. You can restart it from Help or Settings → About.");
  }

  function skip() {
    persist("skipped", state.index);
    close(null);
    UI().toast("Tutorial skipped. You can restart it from Help or Settings → About.");
  }

  function close(status) {
    if (!state.open) return;
    state.open = false;
    state.token += 1;
    const overlay = UI().$("tutorial");
    if (overlay) {
      overlay.classList.add("hidden");
      overlay.setAttribute("aria-hidden", "true");
    }
    document.body.classList.remove("tutorial-active");
    if (state.onScroll) {
      window.removeEventListener("scroll", state.onScroll, true);
      window.removeEventListener("resize", state.onScroll);
      state.onScroll = null;
    }
    if (status) persist(status, state.index);
    const target = state.lastFocus;
    state.lastFocus = null;
    if (target && typeof target.focus === "function") target.focus();
  }

  function start(options) {
    if (state.open) return;
    if (busy()) {
      UI().toast("Wait for the current chat or research run to finish before starting the tour.");
      return;
    }
    state.open = true;
    state.index = 0;
    state.skipped = new Set();
    state.lastFocus = document.activeElement;
    const overlay = UI().$("tutorial");
    if (!overlay) return;
    const card = UI().$("tutorial-card");
    if (card) card.setAttribute("tabindex", "-1");
    overlay.classList.remove("hidden");
    overlay.setAttribute("aria-hidden", "false");
    document.body.classList.add("tutorial-active");
    state.onScroll = () => {
      const step = STEPS[state.index];
      if (!step || !step.target) return;
      const target = document.querySelector(step.target);
      if (target) place(target);
    };
    window.addEventListener("scroll", state.onScroll, true);
    window.addEventListener("resize", state.onScroll);
    go(0);
  }

  function wire() {
    const startButton = UI().$("open-tutorial");
    if (startButton) startButton.addEventListener("click", () => start({ manual: true }));
    const nextButton = UI().$("tutorial-next");
    if (nextButton) nextButton.addEventListener("click", next);
    const backButton = UI().$("tutorial-back");
    if (backButton) backButton.addEventListener("click", back);
    const skipButton = UI().$("tutorial-skip");
    if (skipButton) skipButton.addEventListener("click", skip);

    document.addEventListener("keydown", (event) => {
      if (!state.open) return;
      if (event.key === "Escape") { event.preventDefault(); skip(); }
      if (event.key === "ArrowRight") { event.preventDefault(); next(); }
      if (event.key === "ArrowLeft") { event.preventDefault(); back(); }
      if (event.key === "Tab") trap(event);
    });

    const stored = readState();
    const hash = (window.location.hash || "").replace(/^#/, "");
    const parts = hash.split("&");
    const params = {};
    parts.slice(1).forEach((pair) => {
      const [key, value] = pair.split("=");
      if (key) params[key] = decodeURIComponent(value || "");
    });
    const boot = { view: parts[0] || "run", params };
    if (L().shouldAutoStartTutorial(boot, stored, busy())) {
      window.setTimeout(() => {
        if (!state.open && !UI().$("settings-modal").classList.contains("hidden")) return;
        start({ auto: true });
      }, 900);
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();

  root.AthenaTutorial = {
    start,
    next,
    back,
    skip,
    finish,
    isOpen: () => state.open,
  };
}(typeof globalThis !== "undefined" ? globalThis : this));
