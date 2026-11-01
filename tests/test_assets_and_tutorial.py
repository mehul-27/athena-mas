"""Vendor integrity, Circum icons, Capillary config, tutorial and chat UI contracts."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "athena" / "api" / "static"
VENDOR = STATIC / "vendor" / "asciify"
CIRCUM = STATIC / "assets" / "circum"
CIRCUM_SOURCE = Path(r"M:\Athena\Circum_1.0-Free\SVG")


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# --- Asciify vendor ---------------------------------------------------------


def test_vendor_import_closure_resolves() -> None:
    files = sorted(VENDOR.rglob("*.js"))
    assert files, "vendor/asciify is missing"
    for path in files:
        text = path.read_text(encoding="utf-8")
        specs = re.findall(r'from\s*"([^"]+)"', text) + re.findall(r'import\s*"([^"]+)"', text)
        for spec in specs:
            if spec.startswith("."):
                target = (path.parent / spec).resolve()
                assert target.exists(), f"{path.name} imports missing {spec}"
        assert not re.search(r'from\s*"https?://', text), f"{path.name} imports a remote module"


def test_vendor_entry_exports_only_the_capillary_pieces() -> None:
    source = (VENDOR / "capillary.js").read_text(encoding="utf-8")
    assert "background" in source and "capillary" in source
    assert (VENDOR / "LICENSE").exists()
    source_md = (VENDOR / "SOURCE.md").read_text(encoding="utf-8")
    assert "4.32.8" in source_md and "SHA-256" in source_md


# --- Circum icons -----------------------------------------------------------


def test_every_referenced_circum_symbol_exists() -> None:
    sprite = (CIRCUM / "icons.svg").read_text(encoding="utf-8")
    symbols = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', sprite))
    assert symbols, "the Circum sprite has no symbols"
    referenced: set[str] = set()
    for name in ("index.html", "app.js", "chat.js", "settings.js", "tutorial.js"):
        referenced.update(re.findall(r"#i-([a-z0-9-]+)", _read(name)))
    missing = referenced - symbols
    assert not missing, f"missing icon symbols: {sorted(missing)}"


def test_sprite_uses_current_color_and_no_hardcoded_ink() -> None:
    sprite = (CIRCUM / "icons.svg").read_text(encoding="utf-8")
    assert "currentColor" in sprite
    assert "#1e1e23" not in sprite
    assert (CIRCUM / "LICENSE").exists()
    assert (CIRCUM / "SOURCES.md").exists()


@pytest.mark.skipif(not CIRCUM_SOURCE.exists(), reason="the supplied Circum pack is not present")
def test_circum_sprite_is_current() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_circum_icons.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_icon_only_buttons_have_accessible_names() -> None:
    html = _read("index.html")
    for element_id in (
        "sidebar-toggle", "open-tutorial", "theme-toggle",
        "settings-close", "chat-refresh", "tools-popover-close",
    ):
        block = html[html.index(f'id="{element_id}"'):html.index(f'id="{element_id}"') + 260]
        assert "aria-label=" in block, f"#{element_id} needs an aria-label"


# --- Capillary background ---------------------------------------------------


def test_capillary_host_is_the_interactive_main_surface() -> None:
    html = _read("index.html")
    assert 'id="capillary-host"' not in html
    assert html.count('src="/background.js"') == 1
    assert 'id="main"' in html
    source = _read("background.js")
    assert 'HOST_SELECTOR = ".main"' in source
    assert "document.querySelector(HOST_SELECTOR)" in source
    assert 'import("/vendor/asciify/capillary.js")' in source
    assert "requestIdleCallback" in source
    assert "pagehide" in source
    assert "setEnabled" in source and "pause" in source and "play" in source
    assert "getClientRects" in source and "capillaryOptions(textRects)" in source


def test_capillary_config_and_reduced_motion_surface() -> None:
    logic = _read("logic.js")
    for token in (
        'ink: "#f2f0ea"', 'algorithm: "bayer4"', "scheme: \"dark\"",
        "speed: 1", "size: 10", "opacity: 1", "characters: \"detailed\"",
        "word: \"ASCIIFY\"", "scene: \"flow\"", "hover: \"lantern\"", "mark: \"glyph\"",
    ):
        assert token in logic, f"missing requested Capillary value {token}"
    assert "CAPILLARY_REQUESTED" in logic and "capillaryOptions" in logic
    css = _read("style.css")
    assert "--capillary-ink: #f2f0ea" in css
    assert ".main { position: relative; z-index: 1; background: transparent; }" in css
    assert "prefers-reduced-motion" in css
    assert "fonts.googleapis" not in css and "fonts.gstatic" not in css


def test_geist_fonts_are_local_and_licensed() -> None:
    css = _read("style.css")
    assert '@font-face' in css
    assert "/assets/fonts/geist-latin-variable.woff2" in css
    assert "/assets/fonts/geist-mono-latin-variable.woff2" in css
    assert (STATIC / "assets" / "fonts" / "geist-latin-variable.woff2").exists()
    assert (STATIC / "assets" / "fonts" / "geist-mono-latin-variable.woff2").exists()
    assert (STATIC / "assets" / "fonts" / "LICENSE").exists()
    assert "Geist" in css and "Geist Mono" in css


def test_scrollbars_are_thin_and_neutral() -> None:
    css = _read("style.css")
    assert "scrollbar-width: thin" in css
    assert "*::-webkit-scrollbar" in css
    assert "width: 8px" in css
    assert "#343434" in css
    assert "#00aaff" not in css and "#66c7ff" not in css


# --- Tutorial ---------------------------------------------------------------


def test_tutorial_shell_is_accessible_and_skippable() -> None:
    html = _read("index.html")
    assert 'role="dialog"' in html
    assert 'aria-labelledby="tutorial-title"' in html
    assert 'id="tutorial-back"' in html and 'id="tutorial-skip"' in html and 'id="tutorial-next"' in html
    source = _read("tutorial.js")
    assert "athena.tutorial.v1" in source
    assert '"Escape"' in source and '"ArrowRight"' in source and '"ArrowLeft"' in source
    assert "tutorial-active" in source
    assert "scrollIntoView" in source
    assert "is-mobile" in source
    assert "shouldAutoStartTutorial" in source
    for target in ("#nav-chat", "#chat-model", "#chat-capabilities", "#cap-tools", "#nav-run", "#settings-nav"):
        assert target in source


def test_wordmark_and_empty_state_typography() -> None:
    html = _read("index.html")
    assert 'class="brand-wordmark"' in html
    assert 'class="chat-empty-title"' in html
    assert 'class="chat-empty-name"' in html
    assert 'aria-label="Ask athena anything"' in html
    assert "brand-logo" not in html and "brand-ascii" not in html and "ascii-banner" not in html
    assert '<h2 id="result-title">athena</h2>' in html
    assert "athena" in html
    assert "ATHENA" not in html and "Athena" not in html, "visible page copy must use lowercase athena"
    css = _read("style.css")
    brand = css[css.index(".brand-wordmark {"):css.index("}", css.index(".brand-wordmark {")) + 1]
    assert "var(--font-mono)" in brand and "letter-spacing" in brand
    assert "border-bottom: 1px dotted" in brand
    heading = css[css.index(".chat-empty h1.chat-empty-title {"):css.index("}", css.index(".chat-empty h1.chat-empty-title {")) + 1]
    assert "var(--font-mono)" in heading and "border-bottom: 1px dotted" in heading
    assert "ascii-banner" not in css and "brand-ascii" not in css
    source = _read("chat.js")
    assert 'el("span", "turn-label", "athena")' in source
    assert "athena is thinking" in source
    assert "ATHENA is thinking" not in source
    tutorial = _read("tutorial.js")
    assert "Welcome to athena" in tutorial and "athena has two modes" in tutorial


def test_chat_and_settings_do_not_build_dom_with_innerhtml() -> None:
    for name in ("chat.js", "settings.js", "tutorial.js", "icons.js", "background.js"):
        assert "innerHTML" not in _read(name), f"{name} uses innerHTML"


def test_chat_contract_has_streaming_and_stop() -> None:
    source = _read("chat.js")
    assert "sseParser" in source
    assert "AbortController" in source
    assert "/stop" in source
    assert "aria-pressed" in source
    html = _read("index.html")
    assert 'id="chat-stop"' in html and "hidden" in html[html.index('id="chat-stop"'):html.index('id="chat-stop"') + 120]
    css = _read("style.css")
    start = css.index("#tools-popover {")
    rule = css[start:css.index("}", start) + 1]
    assert "right: 0" in rule and "left: auto" in rule, "the tool picker must open leftward on narrow screens"


def test_settings_modal_contract() -> None:
    html = _read("index.html")
    assert 'id="settings-dialog"' in html
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    assert 'id="settings-close"' in html and 'aria-label="Close settings"' in html
    assert 'id="settings-find"' in html
    css = _read("style.css")
    start = css.index(".settings-modal {")
    rule = css[start:css.index("}", start) + 1]
    assert "min(1120px, 92vw)" in rule and "min(820px, 88vh)" in rule
    assert ".modal-backdrop" in css and "z-index: 70" in css
    assert ".tutorial" in css and "z-index: 80" in css


def test_presentation_size_is_removed_from_the_header() -> None:
    html = _read("index.html")
    assert 'id="presentation-toggle"' not in html
    appbar = html[html.index('class="appbar"'):html.index("</header>")]
    assert "Presentation size" not in appbar
    # The display-size preference still exists in Settings, where it belongs.
    assert 'id="size-choice-presentation"' in html
    source = _read("app.js")
    assert 'getElementById("presentation-toggle")' not in source


def test_sidebar_is_collapsible_and_contextual() -> None:
    html = _read("index.html")
    assert 'id="sidebar-chats"' in html and 'id="sidebar-research"' in html
    assert 'id="sidebar-backdrop"' in html
    assert 'id="open-settings"' not in html
    assert 'id="nav-settings"' in html
    assert 'aria-expanded="true"' in html[html.index('id="sidebar-toggle"'):html.index('id="sidebar-toggle"') + 240]
    source = _read("app.js")
    assert "athena.sidebar" in source and "setSidebar" in source
    assert "sidebar-chats" in source and "sidebar-research" in source
    assert "sidebar-backdrop" in source
