"""Offline tests for scripts/preflight.py (dry run only, no calls)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("preflight", ROOT / "scripts" / "preflight.py")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their module through sys.modules during exec.
    sys.modules["preflight"] = module
    spec.loader.exec_module(module)
    return module


def test_dry_run_reports_hosts_never_keys(monkeypatch, capsys) -> None:
    monkeypatch.setenv("LLM_MODEL", "openai/some-org/some-model")
    monkeypatch.setenv("LLM_API_BASE", "http://gateway.local:8080/v1")
    preflight = _load()

    code = preflight.main([])
    out = capsys.readouterr().out

    assert code == 0
    assert "gateway.local" in out
    assert os.environ["LLM_API_KEY"] not in out
    assert "/v1" not in out, "paths in the base URL must not be printed"
    assert "api key       : present (length" in out
    assert "endpoint guard: active" in out


def test_mismatch_is_flagged(monkeypatch, capsys) -> None:
    monkeypatch.setenv("LLM_MODEL", "openai/some-org/some-model")
    monkeypatch.setenv("LLM_API_BASE", "http://gateway.local:8080/v1")
    preflight = _load()

    def elsewhere():
        return preflight.Path("stub-path", "api.openai.com", "openai/x", "stub"), None

    preflight.BUILDERS = (elsewhere,)
    code = preflight.main([])
    out = capsys.readouterr().out

    assert code == 1
    assert "MISMATCH" in out
    assert "api.openai.com" in out


def test_report_marks_each_bad_path() -> None:
    preflight = _load()
    paths = [
        preflight.Path("good", "gateway.local", "m", "d"),
        preflight.Path("bad", "", "m", "d"),
    ]
    text, mismatch = preflight.report(paths, "gateway.local", True)

    assert mismatch is True
    assert text.count("MISMATCH") == 1
    assert "(provider default)" in text


def test_clean_report_has_no_mismatch() -> None:
    preflight = _load()
    paths = [preflight.Path("good", "gateway.local", "m", "d")]
    _text, mismatch = preflight.report(paths, "gateway.local", True)

    assert mismatch is False
