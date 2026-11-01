/* One managed Capillary renderer for the whole app.
 *
 * The renderer host is `.main`, not a decorative div behind it: the Capillary
 * simulation listens for pointer events on its host, so the host must be the
 * element that actually receives pointer moves. The canvas itself is created
 * with pointer-events:none by the official kit and stays behind all controls.
 *
 * The official Asciify kit is a local ES module (see vendor/asciify/SOURCE.md);
 * a load failure or a browser without WebGL2 leaves Athena fully usable.
 */

const PREF_KEY = "athena.background";
const HOST_SELECTOR = ".main";

const state = {
  handle: null,
  loading: null,
  enabled: readEnabled(),
  paused: false,
};

let clearDirty = true;
let clearCache = [];
let contentObserver = null;

function readEnabled() {
  try { return localStorage.getItem(PREF_KEY) !== "0"; } catch (_) { return true; }
}

function applyDataset(value) {
  document.documentElement.dataset.capillary = value;
}

function hostElement() {
  return document.querySelector(HOST_SELECTOR);
}

const CHAT_TEXT = [
  ".chat-text", ".chat-empty h1", ".chat-empty .empty-copy",
  ".chat-empty .chat-empty-hint", ".chat-empty .eyebrow", ".chat-turn .turn-label",
  ".chat-head .chat-title-btn span", ".chat-meta", ".chat-usage",
  ".tool-row .tool-name", ".tool-row .tool-result", ".tool-row .tool-error",
  ".source-excerpt strong", ".source-excerpt p", ".chat-note", ".chat-error",
];

const RESEARCH_TEXT = [
  ".view-head h1", ".view-head .hint", ".empty-hero .eyebrow", ".empty-hero h1",
  ".empty-copy", ".empty-actions .hint", ".previous-run", ".progress-strip",
  ".turn-label", ".user-turn p", ".answer", ".points li", ".citations", ".sources li",
  ".meta-row .pill", ".report-meta", ".composer-pattern label", ".composer-pattern-note",
  ".card-head h2", ".hint",
];

/* Rects of the actual text lines, not the whole column. The mask protects
   readability while leaving the veins and the lantern light visible between
   lines, in the margins and in empty states. */
function textRects() {
  if (!clearDirty) return clearCache;
  const view = document.querySelector(".main .view:not(.hidden)");
  if (!view) {
    clearCache = [];
    clearDirty = false;
    return clearCache;
  }
  const selectors = view.id === "view-chat" ? CHAT_TEXT : RESEARCH_TEXT;
  const rects = [];
  view.querySelectorAll(selectors.join(",")).forEach((node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const rect of range.getClientRects()) {
      if (rect.width > 1 && rect.height > 1) {
        rects.push({ left: rect.left, top: rect.top, width: rect.width, height: rect.height });
      }
    }
  });
  clearCache = rects;
  clearDirty = false;
  return clearCache;
}

function markFailure(host, error) {
  applyDataset("failed");
  if (host) host.dataset.state = "failed";
  if (window.console && typeof window.console.warn === "function") {
    window.console.warn("Capillary background unavailable:", (error && error.message) || error);
  }
}

function observeContent() {
  if (contentObserver || typeof MutationObserver !== "function") return;
  const main = hostElement();
  if (!main) return;
  contentObserver = new MutationObserver(() => { clearDirty = true; });
  contentObserver.observe(main, { subtree: true, childList: true, characterData: true });
}

function unobserveContent() {
  if (!contentObserver) return;
  contentObserver.disconnect();
  contentObserver = null;
}

async function mount() {
  if (!state.enabled || state.handle) return state.handle;
  if (state.loading) return state.loading;
  const host = hostElement();
  if (!host) return null;
  if (document.body.classList.contains("modal-open")) state.paused = true;
  clearDirty = true;
  observeContent();
  state.loading = import("/vendor/asciify/capillary.js")
    .then(({ background, capillary }) => {
      if (!state.enabled) return null;
      if (typeof background !== "function" || !capillary) {
        throw new Error("capillary exports are missing");
      }
      const options = window.AthenaLogic.capillaryOptions(textRects);
      const handle = background(host, { preset: capillary, ...options });
      if (!handle || handle.supported === false) {
        if (handle && typeof handle.destroy === "function") handle.destroy();
        applyDataset("off");
        host.dataset.state = "off";
        return null;
      }
      state.handle = handle;
      if (state.paused) handle.pause();
      host.dataset.state = "on";
      applyDataset("on");
      return handle;
    })
    .catch((error) => {
      markFailure(host, error);
      return null;
    })
    .finally(() => {
      state.loading = null;
    });
  return state.loading;
}

function pause() {
  state.paused = true;
  if (state.handle) state.handle.pause();
}

function play() {
  state.paused = false;
  if (state.handle) state.handle.play();
}

function destroy() {
  if (state.handle) {
    state.handle.destroy();
    state.handle = null;
  }
  unobserveContent();
  const host = hostElement();
  if (host) host.dataset.state = "off";
}

function setEnabled(on) {
  state.enabled = Boolean(on);
  try { localStorage.setItem(PREF_KEY, on ? "1" : "0"); } catch (_) { /* private mode */ }
  if (state.enabled) {
    mount();
  } else {
    destroy();
    applyDataset("off");
  }
}

function updateClear() {
  clearDirty = true;
  if (state.handle) state.handle.update({ clear: textRects });
}

document.addEventListener("athena:view", updateClear);
document.addEventListener("athena:size", updateClear);
window.addEventListener("scroll", () => { clearDirty = true; }, { capture: true, passive: true });
window.addEventListener("resize", () => { clearDirty = true; });
window.addEventListener("pagehide", destroy);
window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  state.handle = null;
  boot();
});

function boot() {
  if (!state.enabled) {
    applyDataset("off");
    return;
  }
  const start = () => { mount(); };
  if (typeof window.requestIdleCallback === "function") {
    window.requestIdleCallback(start, { timeout: 1200 });
  } else {
    window.setTimeout(start, 0);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}

window.AthenaBackground = {
  mount,
  pause,
  play,
  destroy,
  setEnabled,
  updateClear,
  isEnabled: () => state.enabled,
  handle: () => state.handle,
};
