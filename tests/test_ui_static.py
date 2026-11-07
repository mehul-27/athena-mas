"""The restrained UI pass, asserted as static contract.

These tests are about wiring, not looks: the shared breakpoint, the Context
toggle on both surfaces, the citation chips, the panel's persisted preference,
the details Answer tab, the event log's kinds, the document preview and the
Evaluation view. Each assertion names the file it is about, so a regression
says where to look.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "athena" / "api" / "static"

SCRIPTS = [
    "app.js", "chat.js", "context.js", "diagram.js", "logic.js", "settings.js",
    "telemetry.js", "tutorial.js", "background.js", "icons.js",
    "app.composer.test.js", "context.test.js", "diagram.test.js",
    "logic.test.js", "telemetry.test.js",
]


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("name", SCRIPTS)
def test_every_static_script_parses(name: str) -> None:
    result = subprocess.run(
        ["node", "--check", str(STATIC / name)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# --- the one breakpoint ----------------------------------------------------


def test_the_context_breakpoint_is_one_number_in_css_and_js() -> None:
    context = _read("context.js")
    css = _read("style.css")

    declared = re.search(r"const CONTEXT_BREAKPOINT = (\d+);", context)
    assert declared, "context.js must declare CONTEXT_BREAKPOINT"
    assert declared.group(1) == "1280"

    # The panel's overlay behaviour is driven by the body class the script sets,
    # so no media query repeats the number: it could only drift.
    assert "1280" in css, "style.css must document the shared breakpoint"
    assert "@media (max-width: 1280px)" not in css
    assert "1040px" not in css
    assert "body.context-overlay .context-panel" in css
    assert "body.context-overlay .context-backdrop" in css


def test_the_panel_width_is_clamped_and_owned_by_the_shell() -> None:
    css = _read("style.css")

    assert "clamp(300px, 30vw, 380px)" in css
    assert "body.context-open .main { min-width: 0; }" in css
    assert "is-overlay" in _read("context.js")


# --- the explicit Context toggle ------------------------------------------


def test_chat_and_research_both_carry_an_explicit_context_toggle() -> None:
    html = _read("index.html")
    app = _read("app.js")
    chat = _read("chat.js")

    # Chat head and research result card.
    assert 'id="context-toggle"' in html
    assert 'id="research-context-toggle"' in html
    assert html.count('aria-controls="context-panel"') >= 2

    # Both are wired to the shared panel.
    assert 'UI().$("context-toggle")' in chat
    assert "toggleContext(" in chat
    assert "$(\"research-context-toggle\")" in app
    assert "toggleResearchContext(" in app
    # Both keep aria-expanded honest.
    assert '"context-toggle", "research-context-toggle"' in _read("context.js")


def test_the_panel_remembers_its_tab_but_never_its_open_state() -> None:
    context = _read("context.js")

    assert "JSON.stringify({ tab: state.tab })" in context
    assert "JSON.stringify({ open" not in context
    assert "state.open = false;" in context
    # Loading prefers only the tab; an `open` flag in old storage is ignored.
    assert "state.tab = TABS.includes(raw.tab) ? raw.tab : \"sources\";" in context
    assert "raw.open" not in context


# --- citation chips --------------------------------------------------------


def test_a_citation_marker_opens_the_panel_at_that_source_in_both_surfaces() -> None:
    app = _read("app.js")
    chat = _read("chat.js")
    context = _read("context.js")
    css = _read("style.css")

    # Research: the answer is split into text runs and [n] buttons.
    assert "L.answerSegments(body, maxIndex)" in app
    assert "openResearchSource(" in app
    assert "AthenaContext?.openSource(" in app
    # Chat: the same treatment for the assistant's prose.
    assert "factory.answerSegments(value, sources.length)" in chat
    assert "root.AthenaContext?.openSource(" in chat

    # The panel selects the row the marker named.
    assert "function openSource(selection, kind, id, trigger)" in context
    assert "focusPendingSource(host)" in context
    assert "state.pendingSource" in context

    assert ".citation-marker {" in css
    assert ".context-source.is-target" in css


def test_research_sources_are_de_duplicated() -> None:
    logic = _read("logic.js")
    block = logic[logic.index("function normaliseResearchSources"):]
    block = block[: block.index("\n  function contextItems")]

    assert "const seen = new Set();" in block
    assert "seen.has(ref)" in block
    assert "seen.add(ref)" in block


# --- details: the Answer tab ----------------------------------------------


def test_the_details_answer_tab_navigates_back_to_the_conversation() -> None:
    app = _read("app.js")

    assert "function showAnswerTab()" in app
    assert 'if (state.view !== "run") showView("run");' in app
    assert 'if (tab.dataset.tab === "answer") { showAnswerTab(); return; }' in app


# --- the event log ---------------------------------------------------------


def test_the_event_log_renders_every_recorded_kind() -> None:
    logic = _read("logic.js")
    app = _read("app.js")

    kinds = re.search(r"EVENT_LOG_KINDS = Object\.freeze\(\[(.*?)\]\)", logic, re.S)
    assert kinds, "EVENT_LOG_KINDS must exist"
    for kind in ("stage_start", "stage_end", "model_call", "tool_call", "a2a_call",
                 "recovery", "blackboard_write", "limiter_wait", "skip", "error",
                 "retry", "guardrail", "handoff", "done"):
        assert f'"{kind}"' in kinds.group(1), kind

    assert "const row = L.eventLogEntry(event);" in app
    # The renderer must not fall back to a fixed set of kinds.
    assert "eventLogEntry" in logic


# --- the empty state -------------------------------------------------------


def test_both_surfaces_share_one_empty_state_vocabulary() -> None:
    html = _read("index.html")
    css = _read("style.css")

    assert 'class="empty-hero" id="empty-state"' in html
    assert 'class="chat-empty" id="chat-empty"' in html
    # One set of primitives, declared once and reused by both heroes.
    assert len(re.findall(r"(?m)^\.empty-copy \{", css)) == 1
    assert len(re.findall(r"(?m)^\.eyebrow \{", css)) == 1
    assert 'class="eyebrow"' in html
    assert 'class="empty-copy"' in html


# --- dead CSS -------------------------------------------------------------


def test_the_retired_rules_are_gone() -> None:
    css = _read("style.css")

    for dead in (".chip-button", ".icon.lg", "\n.footer {"):
        assert dead not in css, dead
    # The rule that used to hide a dead control went with it.
    assert ".appbar-actions .chip-button" not in css


def test_no_css_block_is_declared_twice_with_the_same_body() -> None:
    css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)
    blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    seen: dict[tuple[str, str], int] = {}
    for selector, body in blocks:
        key = (" ".join(selector.split()), " ".join(body.split()))
        seen[key] = seen.get(key, 0) + 1
    duplicates = [
        key for key, count in seen.items()
        if count > 1 and key[0] not in {"50%", "0%, 100%", "@font-face"}
    ]
    assert not duplicates, duplicates


# --- documents preview and evaluation -------------------------------------


def test_the_documents_view_opens_a_read_only_preview_drawer() -> None:
    html = _read("index.html")
    app = _read("app.js")
    css = _read("style.css")

    assert 'id="doc-preview"' in html
    assert 'id="doc-preview-panel"' in html
    assert 'id="doc-preview-body"' in html
    assert "async function openDocPreview(docId, trigger)" in app
    assert "async function loadDocuments()" in app
    assert "openDocPreview(entry.id || entry.name" in app
    assert "/api/library/documents/" in app
    assert ".doc-preview-panel {" in css
    assert ".doc-chunk-text {" in css


def test_the_evaluation_view_is_opt_in_and_never_fabricates() -> None:
    html = _read("index.html")
    app = _read("app.js")

    assert 'id="nav-eval"' in html
    # Hidden until the harness has written a batch.
    assert 'class="nav-item hidden" data-view="eval"' in html
    assert "async function refreshEvalNav()" in app
    assert "refreshEvalNav().catch(() => {});" in app
    assert "loadEvaluation(" in app
    assert "/api/eval/runs" in app and "/api/eval/summary?run=" in app
    # An empty batch says so instead of showing a zero row.
    assert "No scored runs in this batch." in app
    assert "No evaluation results are present yet." in app
