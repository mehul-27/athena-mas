"""The Evaluation view's read-only API and the document preview drawer.

Every fixture writes a real results tree or a real Chroma collection into a
temporary directory; the endpoints are then pure functions of what is on disk.
Two properties are asserted throughout: nothing in the results tree is written
or modified, and an empty tree answers "nothing here" instead of inventing
numbers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from athena.api import eval_routes
from athena.api.app import create_app
from evaluation import schema

ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = ROOT / "evaluation" / "dataset.json"
DATASET = schema.load_dataset(DATASET_PATH)


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    return TestClient(create_app())


def _record(item: schema.EvalItem, pattern: str, *, run_key: str, latency: float,
            cost: float | None = 0.01) -> dict:
    """One valid record: the fields the runner writes, nothing invented."""
    payload = {
        "run_key": run_key,
        "run_id": "run-" + run_key,
        "prompt_id": item.id,
        "category": item.category,
        "difficulty": item.difficulty,
        "persona": item.persona,
        "pattern": pattern,
        "context_sharing": schema.CONTEXT_SHARING_BY_PATTERN[pattern],
        "backend": "fixture",
        "config": {"model": "fixture-model", "pattern": pattern},
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:01:00+00:00",
        "latency_s": latency,
        "tokens_in": 1000,
        "tokens_out": 500,
        "est_cost_usd": cost,
        "cost_basis": "priced:fixture" if cost is not None else "unknown_model",
        "tool_calls": 1,
        "evidence_count": 0,
        "terminal_state": "completed",
        "final_answer": "A recorded answer for the fixture.",
        "key_points": ["a recorded answer"],
        "citations": [],
        "evidence": [],
        "critic_status": "approved",
        "critic_approved": True,
    }
    return schema.RunRecord.model_validate(payload).model_dump(mode="json")


def _write_batch(directory: Path, records: list[dict], *, started_at: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "raw.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    (directory / "run_meta.json").write_text(
        json.dumps(
            {
                "plan_id": "fixture-plan",
                "backend": "fixture",
                "model": "fixture-model",
                "started_at": started_at,
                "ended_at": started_at,
                "stopped_reason": None,
            }
        ),
        encoding="utf-8",
    )
    return directory


def _png(path: Path) -> Path:
    """A one-pixel PNG, so the chart route serves a real image."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
        )
    )
    return path


@pytest.fixture()
def results(tmp_path, monkeypatch) -> Path:
    """A results tree with two batches, one chart, and one unreadable line."""
    root = tmp_path / "results"
    early = _write_batch(
        root / "20260101T000000Z",
        [
            _record(DATASET.by_id("gf-01"), "sequential", run_key="early-seq", latency=10.0),
            _record(DATASET.by_id("gf-01"), "p2p", run_key="early-p2p", latency=30.0, cost=None),
        ],
        started_at="2026-01-01T00:00:00+00:00",
    )
    late = _write_batch(
        root / "20260102T000000Z",
        [_record(DATASET.by_id("gf-01"), "sequential", run_key="late-seq", latency=20.0)],
        started_at="2026-01-02T00:00:00+00:00",
    )
    _png(late / "charts" / "latency_by_pattern.png")
    (late / "charts" / "notes.txt").write_text("not an image", encoding="utf-8")
    with (early / "raw.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json}\n")
    monkeypatch.setattr(eval_routes, "RESULTS_ROOT", root)
    monkeypatch.setattr(eval_routes, "DATASET_PATH", DATASET_PATH)
    return root


def _snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- no results ------------------------------------------------------------


def test_an_empty_results_tree_reports_no_batches(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(eval_routes, "RESULTS_ROOT", tmp_path / "absent")
    assert client.get("/api/eval/runs").json() == {
        "root": str(tmp_path / "absent"), "runs": [],
    }
    response = client.get("/api/eval/summary")
    assert response.status_code == 404
    assert "no evaluation results" in response.json()["detail"]


def test_a_batch_without_records_is_not_listed(client, tmp_path, monkeypatch) -> None:
    root = tmp_path / "results"
    (root / "empty").mkdir(parents=True)
    (root / "empty" / "raw.jsonl").write_text("\n", encoding="utf-8")
    monkeypatch.setattr(eval_routes, "RESULTS_ROOT", root)

    assert client.get("/api/eval/runs").json()["runs"] == []


# --- runs ------------------------------------------------------------------


def test_runs_lists_each_batch_newest_first_with_its_real_count(client, results) -> None:
    payload = client.get("/api/eval/runs").json()
    ids = [row["id"] for row in payload["runs"]]

    assert ids == ["20260102T000000Z", "20260101T000000Z"]
    assert [row["runs"] for row in payload["runs"]] == [1, 2]
    assert payload["runs"][0]["model"] == "fixture-model"
    assert payload["runs"][0]["started_at"] == "2026-01-02T00:00:00+00:00"
    # The unreadable line is reported, not silently skipped.
    assert any("not valid JSON" in problem for problem in payload["runs"][1]["problems"])


def test_eval_routes_are_registered_on_the_app(client) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/eval/runs" in paths
    assert "/api/eval/summary" in paths
    assert "/api/eval/chart" in paths
    assert "/api/library/documents/{doc_id}" in paths


# --- summary ---------------------------------------------------------------


def test_summary_reports_the_records_of_the_named_batch(client, results) -> None:
    payload = client.get("/api/eval/summary?run=20260101T000000Z").json()

    assert payload["run"] == "20260101T000000Z"
    assert payload["records"] == 2
    assert payload["scored"] >= 1
    assert payload["meta"]["plan_id"] == "fixture-plan"

    patterns = {row["pattern"]: row for row in payload["patterns"]}
    assert set(patterns) == {"sequential", "p2p"}
    # Recorded values only: the two latencies are 10 s and 30 s.
    assert patterns["sequential"]["latency_median_s"] == 10.0
    assert patterns["p2p"]["latency_median_s"] == 30.0
    # An unpriced run is null, never zero.
    assert patterns["p2p"]["cost_median_usd"] is None
    assert patterns["sequential"]["cost_median_usd"] == 0.01
    # Every row states how many runs it is based on.
    assert patterns["sequential"]["runs"] == 1
    assert patterns["sequential"]["scored"] in (0, 1)
    for row in payload["patterns"]:
        if row["success_rate"] is not None:
            assert 0.0 <= row["success_rate"] <= 1.0


def test_summary_defaults_to_the_newest_batch(client, results) -> None:
    payload = client.get("/api/eval/summary").json()
    assert payload["run"] == "20260102T000000Z"
    assert payload["records"] == 1


def test_summary_names_the_charts_that_exist(client, results) -> None:
    payload = client.get("/api/eval/summary?run=20260102T000000Z").json()
    charts = payload["charts"]

    assert [chart["file"] for chart in charts] == ["latency_by_pattern.png"]
    assert charts[0]["href"].startswith("/api/eval/chart?run=20260102T000000Z&name=")
    # A non-image file in charts/ is not offered as a chart.
    assert all(not chart["file"].endswith(".txt") for chart in charts)


def test_summary_of_an_unknown_batch_is_a_404(client, results) -> None:
    assert client.get("/api/eval/summary?run=nope").status_code == 404
    assert client.get("/api/eval/summary?run=..").status_code == 404


def test_the_summary_quotes_the_no_significance_note(client, results) -> None:
    payload = client.get("/api/eval/summary?run=20260102T000000Z").json()
    assert "no significance test is applied" in payload["note"]


# --- charts ----------------------------------------------------------------


def test_a_chart_is_served_from_the_batch_it_belongs_to(client, results) -> None:
    response = client.get(
        "/api/eval/chart", params={"run": "20260102T000000Z", "name": "latency_by_pattern.png"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_the_chart_route_refuses_anything_outside_the_charts_directory(client, results) -> None:
    for params in (
        {"run": "20260102T000000Z", "name": "../raw.jsonl"},
        {"run": "20260102T000000Z", "name": "..%2f..%2frun_meta.json"},
        {"run": "20260102T000000Z", "name": "/etc/passwd"},
        {"run": "20260102T000000Z", "name": "notes.txt"},
        {"run": "20260102T000000Z", "name": "absent.png"},
        {"run": "nope", "name": "latency_by_pattern.png"},
        {"run": "", "name": "latency_by_pattern.png"},
    ):
        assert client.get("/api/eval/chart", params=params).status_code == 404


def test_reading_results_never_modifies_the_results_tree(client, results) -> None:
    before = _snapshot(results)
    client.get("/api/eval/runs")
    client.get("/api/eval/summary?run=20260102T000000Z")
    client.get("/api/eval/chart", params={"run": "20260102T000000Z",
                                          "name": "latency_by_pattern.png"})
    assert _snapshot(results) == before


# --- document preview ------------------------------------------------------


def _index(tmp_path: Path, monkeypatch, chunks: int) -> None:
    """A Chroma-shaped index at the temporary CHROMA_PATH.

    The store is stubbed because Chroma's persistent client needs a native
    SQLite build this environment may not have; what is exercised is the
    preview's own paging, keying and capping against the collection API.
    """
    chromadb = pytest.importorskip("chromadb")
    target = tmp_path / "chroma"
    target.mkdir(parents=True, exist_ok=True)
    (target / "chroma.sqlite3").write_bytes(b"")
    monkeypatch.setenv("CHROMA_PATH", str(target))
    monkeypatch.setenv("RAG_COLLECTION", "athena_documents")

    rows = [
        (
            "chunk-" + str(index),
            {"document_id": "doc-1", "filename": "report.pdf", "page": index + 1},
            "chunk number " + str(index) + " " + "x" * 40,
        )
        for index in range(chunks)
    ]

    class _Store:
        def get(self, include=None, limit=100, offset=0):
            window = rows[offset: offset + limit]
            return {
                "ids": [row[0] for row in window],
                "metadatas": [row[1] for row in window],
                "documents": [row[2] for row in window],
            }

    class _Client:
        def get_collection(self, name):
            return _Store()

    monkeypatch.setattr(chromadb, "PersistentClient", lambda *a, **k: _Client())


def test_document_preview_returns_capped_chunk_text(client, tmp_path, monkeypatch) -> None:
    _index(tmp_path, monkeypatch, 20)

    payload = client.get("/api/library/documents/doc-1").json()
    assert payload["id"] == "doc-1"
    assert payload["chunk_count"] == 20
    assert len(payload["chunks"]) == 12  # capped
    assert payload["truncated"] is True
    assert payload["pages"][0] == 1
    assert payload["chunks"][0]["text"].startswith("chunk number 0")
    # The cap is in characters too, not only in chunks.
    assert sum(len(chunk["text"]) for chunk in payload["chunks"]) <= 12000


def test_document_preview_of_an_unknown_document_is_a_404(client, tmp_path, monkeypatch) -> None:
    _index(tmp_path, monkeypatch, 3)
    assert client.get("/api/library/documents/missing").status_code == 404


def test_document_preview_without_an_index_is_a_404(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "no-index"))
    assert client.get("/api/library/documents/doc-1").status_code == 404


def test_document_preview_is_capped_and_repeatable(client, tmp_path, monkeypatch) -> None:
    _index(tmp_path, monkeypatch, 4)

    first = client.get("/api/library/documents/doc-1").json()
    second = client.get("/api/library/documents/doc-1").json()
    assert first["truncated"] is False
    assert len(first["chunks"]) == 4
    # Reading the index does not change what it says.
    assert first == second
    # Every chunk keeps its page number from the index metadata.
    assert [chunk["page"] for chunk in first["chunks"]] == [1, 2, 3, 4]


def test_the_document_cap_is_a_character_budget(tmp_path, monkeypatch) -> None:
    """A few long chunks hit the character cap before the chunk cap."""
    from athena.tools.rag.status import document_preview

    _index(tmp_path, monkeypatch, 4)
    store_rows = [
        ("id-1", {"document_id": "long"}, "a" * 9000),
        ("id-2", {"document_id": "long"}, "b" * 9000),
    ]

    class _Store:
        def get(self, include=None, limit=100, offset=0):
            window = store_rows[offset: offset + limit]
            return {
                "ids": [row[0] for row in window],
                "metadatas": [row[1] for row in window],
                "documents": [row[2] for row in window],
            }

    class _Client:
        def get_collection(self, name):
            return _Store()

    import chromadb

    monkeypatch.setattr(chromadb, "PersistentClient", lambda *a, **k: _Client())
    preview = document_preview("long", max_chunks=12, max_chars=12000)
    assert preview is not None
    assert preview["chunk_count"] == 2
    assert preview["truncated"] is True
    assert sum(len(chunk["text"]) for chunk in preview["chunks"]) == 12000
    assert len(preview["chunks"]) == 2


def test_the_documents_view_wires_the_preview_drawer() -> None:
    app_js = (ROOT / "athena" / "api" / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "athena" / "api" / "static" / "index.html").read_text(encoding="utf-8")

    assert "/api/library/documents/" in app_js
    assert 'id="doc-preview"' in html and 'id="doc-preview-body"' in html
    assert "openDocPreview" in app_js and "closeDocPreview" in app_js
