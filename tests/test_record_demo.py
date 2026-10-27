"""Offline tests for scripts/record_demo.py (the pipeline is stubbed)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


recorder = _load("record_demo", ROOT / "scripts" / "record_demo.py")


class _Launcher:
    """Stands in for scripts/demo.py's environment checks."""

    def __init__(self, ready: bool = True, started: bool = True) -> None:
        self.ready = ready
        self.started = started
        self.stopped = False
        self.starts = 0

    def provider_ready(self) -> bool:
        return self.ready

    def start_crew_server(self):
        self.starts += 1
        return object() if self.started else None

    def stop_crew_server(self) -> None:
        self.stopped = True


def _stub_bundle(monkeypatch) -> None:
    """The exporter reads the real event store; keep the naming logic real."""
    import athena.demo.export as export_module

    monkeypatch.setattr(
        export_module,
        "build_bundle",
        lambda run_id: {"run_id": run_id, "summary": {}, "events": [],
                        "blackboard": {}, "metrics": []},
    )


def _fake_run(summary: dict, exit_code: int = 0):
    def run(argv: list[str]) -> int:
        path = Path(argv[argv.index("--json-summary") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary), encoding="utf-8")
        return exit_code

    return run


def test_refuses_without_a_provider(monkeypatch, capsys) -> None:
    monkeypatch.setitem(sys.modules, "demo", _Launcher(ready=False))
    code = recorder.main([])

    assert code == 2
    assert "No provider configured" in capsys.readouterr().out


def test_stops_when_the_server_does_not_start(monkeypatch, capsys) -> None:
    monkeypatch.setitem(sys.modules, "demo", _Launcher(started=False))
    code = recorder.main([])

    assert code == 2
    assert "did not start" in capsys.readouterr().out


def test_a_good_run_is_exported(monkeypatch, tmp_path, capsys) -> None:
    launcher = _Launcher()
    monkeypatch.setitem(sys.modules, "demo", launcher)
    monkeypatch.setattr(recorder, "REPLAY", tmp_path / "replay")
    _stub_bundle(monkeypatch)
    monkeypatch.setattr(recorder, "run_cli",
                        _fake_run({"pattern": "sequential", "degraded": False,
                                   "confidence": 0.8, "approved": True}))

    code = recorder.main(["--keep-bad"])

    assert code == 0, capsys.readouterr().out
    assert launcher.stopped is True, "the server must always be stopped"
    exported = list((tmp_path / "replay").glob("*.json"))
    assert len(exported) == 4, [p.name for p in exported]
    assert "degraded=False" in capsys.readouterr().out


def test_a_degraded_run_is_not_exported(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setitem(sys.modules, "demo", _Launcher())
    monkeypatch.setattr(recorder, "REPLAY", tmp_path / "replay")
    _stub_bundle(monkeypatch)
    monkeypatch.setattr(recorder, "run_cli",
                        _fake_run({"pattern": "p2p", "degraded": True,
                                   "confidence": 0.9, "approved": False}))

    code = recorder.main([])

    assert code == 1
    out = capsys.readouterr().out
    assert "not exported: degraded" in out
    assert list((tmp_path / "replay").glob("*.json")) == []


def test_low_confidence_is_not_exported(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(sys.modules, "demo", _Launcher())
    monkeypatch.setattr(recorder, "REPLAY", tmp_path / "replay")
    _stub_bundle(monkeypatch)
    monkeypatch.setattr(recorder, "run_cli",
                        _fake_run({"pattern": "writer", "degraded": False,
                                   "confidence": 0.4, "approved": True}))

    assert recorder.main([]) == 1
    assert list((tmp_path / "replay").glob("*.json")) == []


def test_keep_bad_allows_it(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(sys.modules, "demo", _Launcher())
    monkeypatch.setattr(recorder, "REPLAY", tmp_path / "replay")
    _stub_bundle(monkeypatch)
    monkeypatch.setattr(recorder, "run_cli",
                        _fake_run({"pattern": "p2p", "degraded": True, "confidence": 0.2}))

    assert recorder.main(["--keep-bad"]) == 0
    assert len(list((tmp_path / "replay").glob("*.json"))) == 4


def test_overwrite_uses_the_plain_names(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(sys.modules, "demo", _Launcher())
    monkeypatch.setattr(recorder, "REPLAY", tmp_path / "replay")
    _stub_bundle(monkeypatch)
    monkeypatch.setattr(recorder, "run_cli",
                        _fake_run({"pattern": "sequential", "degraded": False,
                                   "confidence": 0.9}))

    recorder.main(["--overwrite", "--keep-bad"])

    names = sorted(path.stem for path in (tmp_path / "replay").glob("*.json"))
    assert names == ["hierarchical", "p2p", "parallel", "sequential"]


def test_without_overwrite_names_are_suffixed(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(sys.modules, "demo", _Launcher())
    monkeypatch.setattr(recorder, "REPLAY", tmp_path / "replay")
    _stub_bundle(monkeypatch)
    monkeypatch.setattr(recorder, "run_cli",
                        _fake_run({"pattern": "sequential", "degraded": False,
                                   "confidence": 0.9}))

    recorder.main(["--keep-bad"])

    names = sorted(path.stem for path in (tmp_path / "replay").glob("*.json"))
    assert names == ["hierarchical-new", "p2p-new", "parallel-new", "sequential-new"]


def test_a_failed_subprocess_is_reported(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setitem(sys.modules, "demo", _Launcher())
    monkeypatch.setattr(recorder, "REPLAY", tmp_path / "replay")
    _stub_bundle(monkeypatch)
    monkeypatch.setattr(recorder, "run_cli", _fake_run({}, exit_code=2))

    assert recorder.main([]) == 1
    assert "not exported: exit code 2" in capsys.readouterr().out
