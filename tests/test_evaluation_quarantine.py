from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation import analyze, quarantine, report, runner, schema  # noqa: E402

DATASET = schema.load_dataset(REPO_ROOT / "evaluation" / "dataset.json")
CONFIG = schema.load_config(REPO_ROOT / "evaluation" / "config.json")


def _mark(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / quarantine.MARKER_NAME).write_text("not parsed", encoding="utf-8")


def test_marker_blocks_directory_descendants_and_symlink_aliases(tmp_path):
    marked = tmp_path / "quarantined"
    child = marked / "nested"
    _mark(marked)

    assert quarantine.is_quarantined(marked)
    assert quarantine.is_quarantined(child)
    with pytest.raises(quarantine.QuarantinedPathError):
        quarantine.assert_not_quarantined(child)

    alias = tmp_path / "alias"
    try:
        alias.symlink_to(marked, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this Windows setup")
    assert quarantine.is_quarantined(alias / "nested")


def test_marker_entry_blocks_even_when_not_a_json_file(tmp_path):
    marked = tmp_path / "marked"
    marked.mkdir()
    (marked / quarantine.MARKER_NAME).mkdir()
    assert quarantine.is_quarantined(marked)


def test_analysis_loader_and_writer_reject_marked_results_before_touching_raw(tmp_path):
    marked = tmp_path / "marked"
    _mark(marked)
    (marked / "raw.jsonl").write_text("not json\n", encoding="utf-8")

    with pytest.raises(quarantine.QuarantinedPathError):
        analyze.load_analysis(marked, DATASET)
    output = tmp_path / "analysis-output"
    with pytest.raises(quarantine.QuarantinedPathError):
        analyze.analyse(marked, dataset=DATASET, out_dir=output, charts=False)
    assert not output.exists()


def test_analyzer_cli_returns_two_without_writing_for_marked_result_or_output(
    tmp_path, capsys
):
    marked = tmp_path / "marked"
    _mark(marked)
    (marked / "raw.jsonl").write_text("not json\n", encoding="utf-8")
    output = tmp_path / "analysis-output"

    assert analyze.main([
        "--results", str(marked), "--out", str(output), "--no-charts",
    ]) == 2
    assert "quarantined" in capsys.readouterr().err.lower()
    assert not output.exists()

    normal = tmp_path / "normal"
    normal.mkdir()
    out_in_marked = marked / "generated"
    assert analyze.main([
        "--results", str(normal), "--out", str(out_in_marked), "--no-charts",
    ]) == 2
    assert not out_in_marked.exists()


def test_runner_rejects_marked_output_before_executor_construction(tmp_path, monkeypatch):
    out_dir = tmp_path / "marked"
    _mark(out_dir)

    def unexpected_executor(_spec, _backend):
        raise AssertionError("executor must not be constructed for quarantine")

    monkeypatch.setattr(runner, "build_executor", unexpected_executor)
    with pytest.raises(quarantine.QuarantinedPathError):
        runner.Runner(DATASET, CONFIG, out_dir)


def test_runner_cli_refuses_marked_output_before_preflight(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "marked"
    _mark(out_dir)
    monkeypatch.setattr(runner, "load_env", lambda: None)
    monkeypatch.setattr(
        runner,
        "resolve_provenance",
        lambda: schema.ModelProvenance(adk_model="offline", crew_model="offline"),
    )

    def unexpected_preflight(**_kwargs):
        raise AssertionError("quarantine must be rejected before preflight")

    monkeypatch.setattr(runner, "preflight", unexpected_preflight)
    result = runner.main([
        "--config", str(REPO_ROOT / "evaluation" / "config.json"),
        "--dataset", str(REPO_ROOT / "evaluation" / "dataset.json"),
        "--out", str(out_dir),
    ])
    assert result == runner.EXIT_INVALID
    assert "quarantined" in capsys.readouterr().err.lower()


def test_runner_rechecks_marker_under_lock_before_resume_reads_or_execution(
    tmp_path, monkeypatch
):
    out_dir = tmp_path / "results"
    out_dir.mkdir()
    raw = out_dir / "raw.jsonl"
    raw.write_text("preserve this synthetic record\n", encoding="utf-8")
    entry = CONFIG.entries()[0]
    calls = []

    class MarkOnLock:
        def __init__(self, _path):
            pass

        def __enter__(self):
            _mark(out_dir)

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(runner, "OutputDirectoryLock", MarkOnLock)
    batch = runner.Runner(
        DATASET, CONFIG, out_dir, entries=[entry],
        executor=lambda *_args, **_kwargs: calls.append(True) or {},
    )
    with pytest.raises(quarantine.QuarantinedPathError):
        batch.run()
    assert raw.read_text(encoding="utf-8") == "preserve this synthetic record\n"
    assert calls == []


def test_report_discovery_skips_marked_batch_and_explicit_report_refuses_it(
    tmp_path, capsys
):
    root = tmp_path / "results"
    root.mkdir()
    clean = root / "clean"
    clean.mkdir()
    (clean / "raw.jsonl").write_text("{}\n", encoding="utf-8")
    marked = root / "diagnostic"
    _mark(marked)
    (marked / "raw.jsonl").write_text("not json\n", encoding="utf-8")

    assert report.has_records(marked) is False
    assert report.find_results_dirs(root) == [clean]

    output = tmp_path / "REPORT.md"
    assert report.main([
        "--results", str(marked), "--out", str(output),
    ]) == 2
    assert "quarantined" in capsys.readouterr().err.lower()
    assert not output.exists()


def test_report_output_inside_marked_tree_is_rejected_before_writing(tmp_path):
    marked = tmp_path / "marked"
    _mark(marked)
    output = marked / "REPORT.md"
    with pytest.raises(quarantine.QuarantinedPathError):
        report.write_report(
            results_root=tmp_path / "empty",
            out_path=output,
            dataset=DATASET,
            results_dirs=[],
        )
    assert not output.exists()


def test_report_allows_existing_unmarked_output_and_rejects_marked_root(tmp_path):
    output = tmp_path / "REPORT.md"
    output.write_text("prior report", encoding="utf-8")
    written = report.write_report(
        results_root=tmp_path / "empty",
        out_path=output,
        dataset=DATASET,
        results_dirs=[],
    )
    assert written["batches"] == 0
    assert output.read_text(encoding="utf-8") != "prior report"

    marked_root = tmp_path / "marked-root"
    _mark(marked_root)
    with pytest.raises(quarantine.QuarantinedPathError):
        report.write_report(
            results_root=marked_root,
            out_path=tmp_path / "blocked.md",
            dataset=DATASET,
        )


def test_report_output_symlink_into_quarantine_is_rejected(tmp_path):
    marked = tmp_path / "marked"
    _mark(marked)
    target = marked / "REPORT.md"
    alias = tmp_path / "REPORT.md"
    try:
        alias.symlink_to(target)
    except OSError:
        pytest.skip("file symlinks are unavailable on this Windows setup")
    with pytest.raises(quarantine.QuarantinedPathError):
        report.write_report(
            results_root=tmp_path / "empty",
            out_path=alias,
            dataset=DATASET,
            results_dirs=[],
        )


def test_analyzer_refuses_derived_csv_symlink_into_quarantine(tmp_path):
    marked = tmp_path / "marked"
    _mark(marked)
    target = marked / "preserve.csv"
    target.write_text("do not overwrite", encoding="utf-8")
    out_dir = tmp_path / "analysis"
    out_dir.mkdir()
    alias = out_dir / "runs.csv"
    try:
        alias.symlink_to(target)
    except OSError:
        pytest.skip("file symlinks are unavailable on this Windows setup")

    results = tmp_path / "results"
    results.mkdir()
    (results / "raw.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(quarantine.QuarantinedPathError):
        analyze.analyse(results, dataset=DATASET, out_dir=out_dir, charts=False)
    assert target.read_text(encoding="utf-8") == "do not overwrite"
