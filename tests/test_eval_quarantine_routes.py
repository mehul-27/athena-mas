"""Quarantined evaluation batches stay out of API dashboards and file reads."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from athena.api import eval_routes
from athena.api.app import create_app
from evaluation import schema


ROOT = Path(__file__).resolve().parent.parent


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    return TestClient(create_app())


def _batch(root: Path, name: str, started_at: str) -> Path:
    dataset = schema.load_dataset(ROOT / "evaluation" / "dataset.json")
    item = dataset.items[0]
    record = schema.RunRecord(
        run_key=name,
        prompt_id=item.id,
        category=item.category,
        difficulty=item.difficulty,
        persona=item.persona,
        pattern="sequential",
        context_sharing=schema.CONTEXT_SHARING_BY_PATTERN["sequential"],
        started_at=started_at,
        ended_at=started_at,
        latency_s=1.0,
        terminal_state="completed",
        final_answer="A fixture answer.",
    )
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "raw.jsonl").write_text(
        record.model_dump_json() + "\n", encoding="utf-8"
    )
    (directory / "run_meta.json").write_text(
        json.dumps({"started_at": started_at}), encoding="utf-8"
    )
    return directory


def test_marked_batch_is_hidden_from_runs_and_default_summary(
    tmp_path, monkeypatch
) -> None:
    client = _client(monkeypatch)
    root = tmp_path / "results"
    _batch(root, "clean", "2026-01-01T00:00:00+00:00")
    quarantined = _batch(root, "diagnostic", "2026-01-02T00:00:00+00:00")
    (quarantined / "QUARANTINE.json").write_text("invalid JSON still blocks", encoding="utf-8")
    monkeypatch.setattr(eval_routes, "RESULTS_ROOT", root)

    payload = client.get("/api/eval/runs").json()
    assert [row["id"] for row in payload["runs"]] == ["clean"]
    summary = client.get("/api/eval/summary").json()
    assert summary["run"] == "clean"
    assert summary["records"] == 1


def test_marked_batch_is_rejected_before_summary_or_chart_reads(
    tmp_path, monkeypatch
) -> None:
    client = _client(monkeypatch)
    root = tmp_path / "results"
    quarantined = _batch(root, "diagnostic", "2026-01-02T00:00:00+00:00")
    (quarantined / "QUARANTINE.json").write_text("not parsed", encoding="utf-8")
    charts = quarantined / "charts"
    charts.mkdir()
    (charts / "private.png").write_bytes(b"must not be served")
    monkeypatch.setattr(eval_routes, "RESULTS_ROOT", root)

    summary = client.get("/api/eval/summary?run=diagnostic")
    chart = client.get(
        "/api/eval/chart", params={"run": "diagnostic", "name": "private.png"}
    )
    assert summary.status_code == 404
    assert "quarantined" in summary.json()["detail"]
    assert chart.status_code == 404
    assert "quarantined" in chart.json()["detail"]


def test_quarantined_batch_is_skipped_before_raw_record_probe(
    tmp_path, monkeypatch
) -> None:
    client = _client(monkeypatch)
    root = tmp_path / "results"
    quarantined = _batch(root, "diagnostic", "2026-01-02T00:00:00+00:00")
    (quarantined / "QUARANTINE.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(eval_routes, "RESULTS_ROOT", root)
    original = eval_routes.report.has_records
    probed: list[Path] = []

    def has_records_unless_quarantined(directory: Path) -> bool:
        resolved = directory.resolve()
        assert resolved != quarantined.resolve()
        probed.append(resolved)
        return original(directory)

    monkeypatch.setattr(eval_routes.report, "has_records", has_records_unless_quarantined)

    assert client.get("/api/eval/runs").json()["runs"] == []
    assert quarantined.resolve() not in probed


def test_marker_on_results_ancestor_hides_all_batches(tmp_path, monkeypatch) -> None:
    client = _client(monkeypatch)
    root = tmp_path / "results"
    _batch(root, "clean", "2026-01-01T00:00:00+00:00")
    (root / "QUARANTINE.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(eval_routes, "RESULTS_ROOT", root)

    assert client.get("/api/eval/runs").json()["runs"] == []
    response = client.get("/api/eval/summary?run=clean")
    assert response.status_code == 404
    assert "quarantined" in response.json()["detail"]
