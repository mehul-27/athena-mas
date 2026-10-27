"""UI logic (node), the new read-only endpoints, and the page's static contract."""

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


# --- A8: the pure logic, tested with node ----------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_logic_js_passes_its_node_tests() -> None:
    result = subprocess.run(
        ["node", str(STATIC / "logic.test.js")],
        capture_output=True, text=True, timeout=120, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "# fail 0" in result.stdout


def test_logic_js_has_no_dom_access() -> None:
    source = (STATIC / "logic.js").read_text(encoding="utf-8")
    for forbidden in ("document.", "window.", "innerHTML"):
        assert forbidden not in source, f"logic.js must stay DOM-free ({forbidden})"


# --- A1: replay never opens SSE --------------------------------------------


def test_replay_does_not_open_a_stream() -> None:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    replay_block = source[source.index("async function playReplay"):source.index("function fillCompare")]
    assert "EventSource" not in replay_block, "replay must derive state from recorded events"
    assert "closeStream()" in replay_block


def test_live_stream_has_a_reconnect_limit() -> None:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "MAX_RETRIES = 5" in source
    assert "stream ended" in source


# --- A2: verdict rendering --------------------------------------------------


def test_verdict_is_rendered_as_text_not_an_element() -> None:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "L.verdictLabel" in source
    assert "critic + " not in source, "verdict must come from logic.js"
    assert "critic [object" not in source


# --- A5: inline banner ------------------------------------------------------


def test_banner_is_inline_and_dismissible() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert '<p id="banner"' in html
    banner_rule = css[css.index("#banner {"):css.index("#banner {") + 220]
    assert "position: fixed" not in banner_rule
    assert "cursor: pointer" in banner_rule


# --- A7: projector readability ---------------------------------------------


def test_base_font_and_tables_are_large_enough() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    body = css[css.index("body {"):css.index("body {") + 300]
    table = css[css.index("table {"):css.index("table {") + 120]

    assert "font-size: 19px" in body
    assert "font-size: 18px" in table
    log = css[css.index("#event-log {"):css.index("#event-log {") + 260]
    assert "font-size: 16px" in log


# --- page contract ----------------------------------------------------------


def test_page_has_the_new_panels() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for element_id in ("banner", "toggle-raw", "history-body", "tools-list",
                       "tab-history", "tab-tools", "timeline-body", "compare-body"):
        assert f'id="{element_id}"' in html, f"missing #{element_id}"


def test_page_still_has_no_external_assets() -> None:
    for name in ("index.html", "style.css", "app.js", "logic.js"):
        content = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://", content), f"{name} references a remote URL"


def test_replay_index_marks_bundles_that_need_rerecording(client: TestClient) -> None:
    bundles = client.get("/api/replay").json()["bundles"]
    p2p = [bundle for bundle in bundles if bundle["name"] == "p2p"]
    assert p2p and p2p[0]["note"], "the p2p bundle must say it needs re-recording"


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

    monkeypatch.setattr(app_module._tool_catalog, "__defaults__", None, raising=False)
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
