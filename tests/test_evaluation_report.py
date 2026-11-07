"""``evaluation/REPORT.md`` must be built from the records that exist, and say so
when there are none.

The fixtures are synthetic and live in temporary directories; the only repository
file this module reads is the committed ``evaluation/REPORT.md``, to check that it
is consistent with whatever results exist at the time the test runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation import analyze, report, schema, scoring  # noqa: E402

DATASET = schema.load_dataset(REPO_ROOT / "evaluation" / "dataset.json")
CONFIG = schema.load_config(REPO_ROOT / "evaluation" / "config.json")
CONTEXT_SHARING = schema.CONTEXT_SHARING_BY_PATTERN

GOOD = "gf-01"
RAG = "dr-01"


# --- Fixtures --------------------------------------------------------------


def make_record(
    item: schema.EvalItem,
    pattern: str,
    *,
    run_key: str,
    latency: float = 30.0,
    cost: float | None = 0.01,
    good: bool = True,
    prompt_id: str | None = None,
) -> dict:
    """A complete, valid record: the same shape the runner writes."""
    parts = [group.split("|")[0].strip() for group in item.answer_key.must_mention]
    parts += [f"{expectation.value:g}" for expectation in item.answer_key.numeric]
    answer = None
    evidence: list[dict] = []
    citations: list[dict] = []
    if good:
        answer = (
            f"As of 2025 the answer is {', '.join(part for part in parts if part)}. "
            "This is stated in the sources captured for this run."
        )
        for index, token in enumerate(item.required_evidence):
            if token == "none":
                continue
            if token.startswith("rag:"):
                source_type, reference = "rag", token.split(":", 1)[1]
            elif token == "web":
                source_type, reference = "web", "https://example.org/source"
            elif token in {"calculation", "forecast"}:
                source_type, reference = "calculation", "calc:step-1"
            else:
                source_type, reference = "web", "https://example.org/any"
            evidence.append(
                {
                    "id": f"ev-{index}",
                    "source_type": source_type,
                    "source_ref": reference,
                    "content": answer,
                    "score": 0.9,
                    "retrieved_by": "fixture",
                }
            )
            citations.append(
                {
                    "source_type": source_type,
                    "source_ref": reference,
                    "quote_or_summary": answer[:160],
                }
            )

    key_points = list(parts)
    for sentence in (answer or "").split(". "):
        if len(key_points) >= 4:
            break
        if sentence.strip():
            key_points.append(sentence.strip())
    while good and len(key_points) < 4:
        key_points.append(f"supporting detail {len(key_points) + 1}")

    payload = {
        "run_key": run_key,
        "run_id": f"run-{run_key}",
        "prompt_id": prompt_id or item.id,
        "category": item.category,
        "difficulty": item.difficulty,
        "persona": item.persona,
        "pattern": pattern,
        "context_sharing": CONTEXT_SHARING[pattern],
        "backend": "fixture",
        "config": {"model": "fixture-model", "pattern": pattern, "timeout_s": 60.0},
        "started_at": "2025-01-01T00:00:00+00:00",
        "ended_at": "2025-01-01T00:00:30+00:00",
        "latency_s": latency,
        "tokens_in": 1200,
        "tokens_out": 400,
        "est_cost_usd": cost,
        "cost_basis": "priced:fixture" if cost is not None else "unknown_model",
        "tool_calls": 2,
        "recovery_rounds": 0,
        "revision_rounds": 0,
        "evidence_count": len(evidence),
        "terminal_state": "completed" if good else "failed",
        "final_answer": answer,
        "key_points": key_points if good else [],
        "citations": citations,
        "evidence": evidence,
        "confidence": 0.8 if good else None,
        "critic_status": "approved" if good else "rejected",
        "critic_approved": good,
        "errors": [] if good else ["fixture: intentional failure"],
    }
    return schema.RunRecord.model_validate(payload).model_dump(mode="json")


def write_batch(
    directory: Path,
    records: list[dict],
    *,
    started_at: str = "2025-01-01T00:00:00+00:00",
    plan_id: str = "fixture-plan",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "raw.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    (directory / "run_meta.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "backend": "fixture",
                "model": "fixture-model",
                "started_at": started_at,
                "ended_at": "2025-01-01T01:00:00+00:00",
                "stopped_reason": None,
                "pricing": {
                    "source": "athena.observability.cost",
                    "table": "data/pricing.json",
                    "as_of": "2025-01-01",
                    "entries": 30,
                },
                "pricing_summary": {
                    "model": "fixture-model",
                    "priced": True,
                    "basis": "config.pricing:fixture-model",
                },
            }
        ),
        encoding="utf-8",
    )
    return directory


def two_pattern_batch(root: Path) -> Path:
    return write_batch(
        root / "batch-a",
        [
            make_record(DATASET.by_id(GOOD), "sequential", run_key="a-seq-0", latency=10.0),
            make_record(DATASET.by_id(RAG), "p2p", run_key="a-p2p-0", latency=40.0, cost=None),
        ],
    )


# --- Discovery -------------------------------------------------------------


def test_find_results_dirs_skips_directories_without_records(tmp_path):
    with_records = write_batch(
        tmp_path / "batch", [make_record(DATASET.by_id(GOOD), "sequential", run_key="k1")]
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "raw.jsonl").write_text("\n\n", encoding="utf-8")
    (tmp_path / "no-raw").mkdir()

    assert report.find_results_dirs(tmp_path) == [with_records]


def test_find_results_dirs_orders_batches_by_start_time(tmp_path):
    late = write_batch(
        tmp_path / "z-late",
        [make_record(DATASET.by_id(GOOD), "p2p", run_key="late")],
        started_at="2025-02-01T00:00:00+00:00",
    )
    early = write_batch(
        tmp_path / "a-early",
        [make_record(DATASET.by_id(GOOD), "sequential", run_key="early")],
        started_at="2025-01-01T00:00:00+00:00",
    )
    assert report.find_results_dirs(tmp_path) == [early, late]


def test_a_results_root_that_does_not_exist_yields_no_batches(tmp_path):
    assert report.find_results_dirs(tmp_path / "absent") == []


# --- No results ------------------------------------------------------------


def test_a_report_without_results_states_that_plainly(tmp_path):
    out = tmp_path / "REPORT.md"
    written = report.write_report(
        results_root=tmp_path / "results",
        out_path=out,
        dataset=DATASET,
        config=CONFIG,
    )
    text = out.read_text(encoding="utf-8")

    assert written["records"] == 0
    assert written["batches"] == 0
    assert "no runs have been recorded yet" in text
    # No result tables may appear: they would have to be invented.
    assert "## By pattern" not in text
    assert "## Citation quality" not in text
    assert "NaN" not in text and "n/a" not in text
    # The plan metadata it does state is real and checkable.
    assert str(len(CONFIG.plan.entries)) in text
    assert "scripts/dev_up.py" in text


def test_a_batch_that_recorded_nothing_counts_as_no_results(tmp_path):
    empty = tmp_path / "results" / "batch-empty"
    empty.mkdir(parents=True)
    (empty / "raw.jsonl").write_text("", encoding="utf-8")

    out = tmp_path / "REPORT.md"
    written = report.write_report(
        results_root=tmp_path / "results", out_path=out, dataset=DATASET, config=CONFIG
    )
    assert written["records"] == 0
    assert "no runs have been recorded yet" in out.read_text(encoding="utf-8")


# --- With results ----------------------------------------------------------


def test_a_report_with_results_uses_the_records_it_read(tmp_path):
    root = tmp_path / "results"
    batch = two_pattern_batch(root)
    out = tmp_path / "REPORT.md"
    written = report.write_report(
        results_root=root, out_path=out, dataset=DATASET, config=CONFIG
    )
    text = out.read_text(encoding="utf-8")

    assert written["records"] == 2
    assert written["batches"] == 1

    # The success figure must be the one the scorer produces from these records.
    scores, _problems = scoring.score_records(
        [json.loads(line) for line in (batch / "raw.jsonl").read_text().splitlines() if line],
        DATASET,
    )
    hits, total, share = analyze.rate(score.success for score in scores)
    assert f"{hits}/{total} ({share * 100:.0f}%)" in text

    assert "## By pattern" in text
    assert "sequential" in text and "p2p" in text
    assert "## By category" in text
    assert "## Failure taxonomy" in text
    assert "## Cost accounting" in text
    assert analyze.NO_SIGNIFICANCE_NOTE in text
    # Per-run appendix: every run key is listed.
    assert "a-seq-0" in text and "a-p2p-0" in text
    # Provenance: the batch is named, with the model label it recorded.
    assert "fixture-model" in text
    assert "| Models used | fixture-model |" in text
    assert report._rel(batch) in text


def test_unpriced_runs_are_named_as_a_caveat_not_zeroed(tmp_path):
    root = tmp_path / "results"
    write_batch(
        root / "batch",
        [
            make_record(DATASET.by_id(GOOD), "sequential", run_key="priced-1", cost=0.02),
            make_record(DATASET.by_id(GOOD), "p2p", run_key="unpriced-1", cost=None),
        ],
    )
    out = tmp_path / "REPORT.md"
    report.write_report(results_root=root, out_path=out, dataset=DATASET, config=CONFIG)
    text = out.read_text(encoding="utf-8")

    assert "carry **no cost estimate**" in text
    # "1 of 2" priced: the unpriced run is excluded from the cost statistics.
    assert "1 of 2" in text
    assert "0.02 USD" in text


def test_every_batch_is_listed_and_unreadable_records_are_reported(tmp_path):
    root = tmp_path / "results"
    write_batch(
        root / "batch-a",
        [make_record(DATASET.by_id(GOOD), "sequential", run_key="a-1")],
        started_at="2025-01-01T00:00:00+00:00",
    )
    write_batch(
        root / "batch-b",
        [make_record(DATASET.by_id(GOOD), "p2p", run_key="b-1")],
        started_at="2025-02-01T00:00:00+00:00",
    )
    # An orphan prompt id is a load problem, not a silent drop.
    orphan = make_record(
        DATASET.by_id(GOOD), "p2p", run_key="b-orphan", prompt_id="no-such-prompt"
    )
    with (Path(root) / "batch-b" / "raw.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(orphan) + "\n")

    out = tmp_path / "REPORT.md"
    written = report.write_report(
        results_root=root, out_path=out, dataset=DATASET, config=CONFIG
    )
    text = out.read_text(encoding="utf-8")

    assert written["batches"] == 2
    assert written["records"] == 3
    assert written["scored"] == 2  # the orphan prompt id cannot be scored
    assert any("unknown prompt id" in problem for problem in written["problems"])
    assert "## Problems recorded while loading" in text
    assert "batch-a" in text and "batch-b" in text


def test_volatile_and_security_sections_appear_when_such_items_ran(tmp_path):
    root = tmp_path / "results"
    write_batch(
        root / "batch",
        [
            make_record(DATASET.by_id("cv-01"), "sequential", run_key="cv-1"),
            make_record(DATASET.by_id("pi-01"), "p2p", run_key="pi-1"),
            make_record(DATASET.by_id("tp-02"), "parallel", run_key="tp-1"),
        ],
    )
    out = tmp_path / "REPORT.md"
    report.write_report(results_root=root, out_path=out, dataset=DATASET, config=CONFIG)
    text = out.read_text(encoding="utf-8")

    assert "volatile items" in text
    assert "## Security behaviour" in text
    assert "Prompt-injection items scored" in text
    assert "Toxic-persona items scored" in text


def test_report_is_regenerated_rather_than_appended(tmp_path):
    root = tmp_path / "results"
    batch = write_batch(
        root / "batch",
        [make_record(DATASET.by_id(GOOD), "sequential", run_key="first")],
    )
    out = tmp_path / "REPORT.md"
    first = report.write_report(
        results_root=root, out_path=out, dataset=DATASET, config=CONFIG
    )
    first_text = out.read_text(encoding="utf-8")

    with (batch / "raw.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(make_record(DATASET.by_id(RAG), "p2p", run_key="second")) + "\n"
        )
    second = report.write_report(
        results_root=root, out_path=out, dataset=DATASET, config=CONFIG
    )
    second_text = out.read_text(encoding="utf-8")

    assert first["records"] == 1 and second["records"] == 2
    assert "second" not in first_text
    assert "second" in second_text


def test_write_report_touches_only_the_output_file(tmp_path):
    root = tmp_path / "results"
    batch = write_batch(root / "batch", [make_record(DATASET.by_id(GOOD), "sequential", run_key="x")])
    before = sorted(path.name for path in batch.rglob("*"))

    out = tmp_path / "nested" / "REPORT.md"
    report.write_report(results_root=root, out_path=out, dataset=DATASET, config=CONFIG)

    assert out.is_file()
    assert sorted(path.name for path in batch.rglob("*")) == before


def test_with_artifacts_writes_the_per_batch_csv_and_report(tmp_path):
    root = tmp_path / "results"
    batch = write_batch(
        root / "batch", [make_record(DATASET.by_id(GOOD), "sequential", run_key="x")]
    )
    written = report.write_report(
        results_root=root,
        out_path=tmp_path / "REPORT.md",
        dataset=DATASET,
        config=CONFIG,
        with_artifacts=True,
    )
    assert str(batch) in written["artifacts"]
    for name in ("runs.csv", "scored.jsonl", "REPORT.md"):
        assert (batch / name).is_file(), name


def test_cli_writes_the_report_and_can_print_json(tmp_path, capsys):
    root = tmp_path / "results"
    write_batch(root / "batch", [make_record(DATASET.by_id(GOOD), "sequential", run_key="x")])
    out = tmp_path / "REPORT.md"

    code = report.main(["--root", str(root), "--out", str(out), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["records"] == 1
    assert out.is_file()


def test_cli_rejects_an_invalid_dataset(tmp_path, capsys):
    broken = tmp_path / "dataset.json"
    broken.write_text("{}", encoding="utf-8")
    code = report.main(
        ["--root", str(tmp_path), "--out", str(tmp_path / "R.md"), "--dataset", str(broken)]
    )
    assert code == 2
    assert "dataset is not valid" in capsys.readouterr().err


# --- The committed artefact -------------------------------------------------


def test_the_committed_report_matches_the_state_of_the_results_tree():
    """`evaluation/REPORT.md` must be truthful about whether results exist."""
    committed = REPO_ROOT / "evaluation" / "REPORT.md"
    assert committed.is_file(), "evaluation/REPORT.md is a deliverable"

    text = committed.read_text(encoding="utf-8")
    has_batches = bool(report.find_results_dirs(REPO_ROOT / "evaluation" / "results"))

    assert "Athena evaluation report" in text
    assert ("no runs have been recorded yet" in text) is not has_batches
    if not has_batches:
        assert "## By pattern" not in text
