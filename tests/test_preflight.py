"""Offline tests for scripts/preflight.py and its probe results (no calls)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


preflight_probes = _load("preflight_probes")
preflight = _load("preflight")

FakeKey = "sk-abcdefghijklmnop1234"


class AuthenticationError(Exception):
    """Stands in for the provider's auth error."""


def _info(name="stub", host="gateway.local"):
    return preflight_probes.PathInfo(name, host, "openai/x", "stub detail")


def _ok_probe(**kwargs):
    base = dict(path="crewai-plain", host="gateway.local", reply_chars=12,
                tokens_in=40, tokens_out=9, calls=1)
    base.update(kwargs)
    return lambda: preflight_probes.ProbeResult(**base)


def test_dry_run_reports_hosts_never_keys(monkeypatch, capsys) -> None:
    monkeypatch.setenv("LLM_MODEL", "openai/some-org/some-model")
    monkeypatch.setenv("LLM_API_BASE", "http://gateway.local:8080/v1")

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
    monkeypatch.setattr(
        preflight, "describe_paths",
        lambda: [(_info("stub-path", "api.openai.com"), _ok_probe())],
    )

    code = preflight.main([])
    out = capsys.readouterr().out

    assert code == 1
    assert "MISMATCH" in out
    assert "api.openai.com" in out


def test_table_marks_each_bad_path() -> None:
    paths = [_info("good"), _info("bad", "")]
    text, mismatch = preflight.table(paths, "gateway.local", True)

    assert mismatch is True
    assert text.count("MISMATCH") == 1
    assert "(provider default)" in text


def test_clean_table_has_no_mismatch() -> None:
    _text, mismatch = preflight.table([_info("good")], "gateway.local", True)
    assert mismatch is False


def test_failing_probe_shows_the_real_error_and_continues(capsys) -> None:
    def auth_failure():
        raise AuthenticationError(f"Incorrect API key provided: {FakeKey}")

    probes = [
        (_info("adk-lite-llm"), auth_failure),
        (_info("crewai-plain"), _ok_probe()),
    ]
    code = preflight.run_live(probes)
    out = capsys.readouterr().out

    assert code != 0
    assert "FAILED AuthenticationError" in out
    assert "Incorrect API key provided" in out
    assert FakeKey not in out, "the key must be redacted"
    assert "[REDACTED]" in out
    assert "1 of 2 paths ok" in out
    assert "ok (12 chars)" in out, "the second probe still ran"


def test_success_probe_prints_real_numbers(capsys) -> None:
    probes = [
        (_info("crewai-plain"), _ok_probe(tokens_in=120, tokens_out=30, calls=2)),
        (_info("adk-lite-llm"), _ok_probe(path="adk-lite-llm", reasoning_tokens=7)),
    ]
    code = preflight.run_live(probes)
    out = capsys.readouterr().out

    assert code == 0
    assert "2 of 2 paths ok" in out
    assert "tokens 120/30" in out
    assert "calls 2" in out


def test_zero_tokens_with_a_reply_is_flagged(capsys) -> None:
    probes = [(_info("crewai-structured"), _ok_probe(path="crewai-structured",
                                                    tokens_in=0, tokens_out=0, calls=0))]
    code = preflight.run_live(probes)
    out = capsys.readouterr().out

    assert code != 0
    assert "USAGE NOT CAPTURED" in out
    assert "0 of 1 paths ok" in out


def test_zero_token_adk_probe_is_not_flagged(capsys) -> None:
    """The crewai rule does not apply to the ADK path."""
    probes = [(_info("adk-lite-llm"), _ok_probe(path="adk-lite-llm",
                                                tokens_in=0, tokens_out=0, calls=0))]
    code = preflight.run_live(probes)
    assert code == 0


def test_result_always_has_every_field() -> None:
    result = preflight_probes.failure("p", "h", ValueError("boom"))

    assert result.ok is False
    assert result.error_type == "ValueError"
    assert result.tokens_in == 0 and result.calls == 0
    assert preflight.result_line(result).startswith("  p ")
    assert "FAILED ValueError: boom" in preflight.result_line(result)
