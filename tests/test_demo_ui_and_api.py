"""UI logic (node), the read-only endpoints, and the page's static contract."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from athena.api.app import create_app

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "athena" / "api" / "static"


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    return TestClient(create_app())


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# --- the pure logic layer ---------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_logic_js_passes_its_node_tests() -> None:
    result = subprocess.run(["node", str(STATIC / "logic.test.js")],
                            capture_output=True, text=True, timeout=120, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "# fail 0" in result.stdout


def test_logic_js_has_no_dom_access() -> None:
    source = _read("logic.js")
    for forbidden in ("document.", "window.", "innerHTML", "getElementById"):
        assert forbidden not in source, f"logic.js must stay DOM-free ({forbidden})"


def test_app_js_never_uses_innerhtml() -> None:
    for name in ("app.js", "diagram.js"):
        assert "innerHTML" not in _read(name), f"{name} builds DOM with innerHTML"


# --- run view ---------------------------------------------------------------


def test_replay_does_not_open_a_stream() -> None:
    source = _read("app.js")
    block = source[source.index("async function openReplay"):source.index("async function openRun")]
    assert "EventSource" not in block, "replay must derive state from recorded events"
    assert "beginRun(bundle.run_id, true)" in block


def test_live_stream_has_a_reconnect_limit() -> None:
    source = _read("app.js")
    assert "MAX_RETRIES = 5" in source
    assert "stream ended" in source


def test_verdict_is_rendered_as_text() -> None:
    source = _read("app.js")
    assert "L.verdictLabel" in source
    assert "critic + " not in source, "verdict must come from logic.js"


def test_banner_is_inline_and_dismissible() -> None:
    html, css = _read("index.html"), _read("style.css")
    assert '<p id="banner"' in html
    rule = css[css.index(".banner {"):css.index(".banner {") + 260]
    assert "position: fixed" not in rule
    assert "cursor: pointer" in rule


def test_presentation_size_raises_the_base_font() -> None:
    css = _read("style.css")
    assert "--base: 19px" in css
    assert 'html[data-size="presentation"]' in css
    assert "font: var(--base)" in css


def test_light_theme_exists() -> None:
    css = _read("style.css")
    assert 'html[data-theme="light"]' in css
    assert "--bg: #eef1f5" in css


# --- architecture diagram ---------------------------------------------------


def test_diagram_module_renders_an_accessible_svg() -> None:
    source = _read("diagram.js")
    assert '"title"' in source and '"desc"' in source
    assert "aria-labelledby" in source
    assert "marker-end" in source, "edges need arrowheads"
    assert "LANES" in source and "CrewAI" in source and "MCP" in source


def test_diagram_has_live_state_styles_and_reduced_motion() -> None:
    css = _read("style.css")
    assert ".node.state-running" in css
    assert ".node.state-failed" in css
    assert "prefers-reduced-motion" in css


# --- page contract ----------------------------------------------------------


def test_page_has_the_panels() -> None:
    html = _read("index.html")
    for element_id in ("sidebar", "status-pill", "question", "run-button", "live-map",
                       "timeline-body", "event-log", "blackboard-sections", "result-card",
                       "report-answer", "heatmap", "compare-body", "history-list",
                       "replay-list", "how-diagram", "view-settings", "profiles-body",
                       "profile-key", "profile-test", "profile-activate", "test-result",
                       "about-me", "view-documents", "footer-run"):
        assert f'id="{element_id}"' in html, f"missing #{element_id}"


def test_sidebar_has_every_view() -> None:
    html = _read("index.html")
    for view in ("run", "how", "documents", "settings"):
        assert f'id="view-{view}"' in html
        assert f'data-view="{view}"' in html


def test_the_key_field_never_keeps_a_key() -> None:
    html = _read("index.html")
    field = html[html.index('id="profile-key"'):html.index('id="profile-key"') + 120]
    assert 'type="password"' in field
    assert 'autocomplete="new-password"' in field

    source = _read("app.js")
    assert "never repopulate a stored key" in source
    assert 'profile-key").value = ""' in source


def test_reasoning_effort_defaults_to_not_sent() -> None:
    html = _read("index.html")
    start = html.index('id="profile-reasoning"')
    select = html[start:html.index("</select>", start)]
    assert "Default (don't send)" in select
    assert select.index('value=""') < select.index('value="low"')


def test_page_has_no_remote_assets() -> None:
    for name in ("index.html", "style.css", "app.js", "logic.js", "diagram.js"):
        content = _read(name)
        remote = re.findall(
            r"(?:src|href)=[\x22\x27]https?://|url\(\s*[\x22\x27]?https?://|@import", content)
        assert not remote, f"{name} loads a remote asset"


def test_deep_links_are_supported() -> None:
    source = _read("app.js")
    assert 'hash.startsWith("replay=")' in source
    assert 'hash === "settings"' in source
    assert 'hash === "how"' in source


def test_page_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Athena" in response.text


def test_replay_index_marks_bundles_that_need_rerecording(client: TestClient) -> None:
    bundles = client.get("/api/replay").json()["bundles"]
    p2p = [bundle for bundle in bundles if bundle["name"] == "p2p"]
    assert p2p and p2p[0]["note"]


# --- read-only endpoints ----------------------------------------------------


def test_tools_panel_is_cached_and_read_only(client: TestClient, monkeypatch) -> None:
    import athena.api.app as app_module

    calls: list = []

    class Done:
        returncode = 0
        stdout = json.dumps({"servers": [
            {"name": "rag (MCP)", "tools": [{"name": "rag_search", "description": "search"}]}
        ]})

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Done()

    monkeypatch.setitem(app_module.__dict__, "_TOOLS_CACHE", None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    first = client.get("/api/tools").json()["servers"]
    second = client.get("/api/tools").json()["servers"]

    assert first == second
    assert first[0]["tools"][0]["name"] == "rag_search"
    assert len(calls) == 1, "the catalogue is cached for the session"
    assert calls[0][1] == "scripts/list_tools.py"


def test_history_lists_runs_with_summaries(client: TestClient, monkeypatch) -> None:
    import athena.api.app as app_module

    monkeypatch.setitem(app_module.__dict__, "_TOOLS_CACHE", None)
    monkeypatch.setattr(app_module, "_store", lambda: _FakeStore())

    runs = client.get("/api/runs").json()["runs"]

    assert runs == [{
        "run_id": "r1", "question": "", "pattern": "sequential", "started_at": "",
        "confidence": 0.8, "approved": True, "severity": "low", "degraded": False,
    }]


class _FakeStore:
    def runs(self) -> list[str]:
        return ["r1"]

    def events(self, run_id: str):
        return [_Event()]


class _Event:
    payload_summary = "run_summary sequential"
    extra = {"pattern": "sequential", "confidence": 0.8, "approved": True,
             "severity": "low", "degraded": False}
    seq = 1
