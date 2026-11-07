"""The analyser must aggregate real records, chart them, and never invent a number.

Every fixture in this file is synthetic and written to a temporary directory. No
test reads ``evaluation/results/`` and no test starts a run: the point here is the
arithmetic and the honesty of the output, not the pipeline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation import analyze, schema, scoring  # noqa: E402

DATASET = schema.load_dataset(REPO_ROOT / "evaluation" / "dataset.json")
CONTEXT_SHARING = schema.CONTEXT_SHARING_BY_PATTERN

#: A small, fixed selection so the tests are cheap and the expected numbers are
#: obvious. Every item comes from the real dataset.
GOOD = "gf-01"
RAG = "dr-01"
CALC = "nc-01"


# --- Fixture builders ------------------------------------------------------


def answer_for(item: schema.EvalItem) -> str:
    """An answer that states the answer key, dated, so it can be scored."""
    parts = [group.split("|")[0].strip() for group in item.answer_key.must_mention]
    parts += [f"{expectation.value:g}" for expectation in item.answer_key.numeric]
    body = ", ".join(part for part in parts if part) or "the requested finding"
    return (
        f"As of 2025 the answer is {body}. "
        "This is stated in the sources captured for this run."
    )


def evidence_for(
    item: schema.EvalItem, answer: str
) -> tuple[list[dict], list[dict]]:
    """One evidence item and one citation per required evidence kind."""
    evidence: list[dict] = []
    citations: list[dict] = []
    for index, token in enumerate(item.required_evidence):
        if token == "none":
            continue
        if token.startswith("rag:"):
            source_type, reference = "rag", token.split(":", 1)[1]
        elif token == "web":
            source_type, reference = "web", "https://example.org/source"
        elif token in {"calculation", "forecast"}:
            source_type, reference = "calculation", "calc:step-1"
        else:  # 'any'
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
    return evidence, citations


def make_record(
    item: schema.EvalItem,
    pattern: str,
    *,
    good: bool = True,
    latency: float = 30.0,
    cost: float | None = 0.01,
    tokens_in: int | None = 1000,
    tokens_out: int | None = 500,
    terminal_state: str | None = None,
    run_key: str | None = None,
    prompt_id: str | None = None,
) -> dict:
    """A complete, valid run record for one dataset item."""
    state = terminal_state or ("completed" if good else "failed")
    answer = answer_for(item) if good else None
    evidence, citations = evidence_for(item, answer or "") if good else ([], [])
    key_points = [group.split("|")[0].strip() for group in item.answer_key.must_mention]
    key_points += [f"{expectation.value:g}" for expectation in item.answer_key.numeric]
    # Several rubrics require three or more key points; pad with the answer's own
    # sentences so the fixture is a genuinely structured report.
    for sentence in (answer or "").split(". "):
        if len(key_points) >= 4:
            break
        if sentence.strip():
            key_points.append(sentence.strip())
    while len(key_points) < 4:
        key_points.append(f"supporting detail {len(key_points) + 1}")
    if not good:
        key_points = []
    payload = {
        "run_key": run_key or f"{item.id}__{pattern}__r0",
        "run_id": f"run-{item.id}-{pattern}",
        "prompt_id": prompt_id or item.id,
        "category": item.category,
        "difficulty": item.difficulty,
        "persona": item.persona,
        "pattern": pattern,
        "context_sharing": CONTEXT_SHARING[pattern],
        "backend": "fixture",
        "started_at": "2025-01-01T00:00:00+00:00",
        "ended_at": "2025-01-01T00:00:30+00:00",
        "latency_s": latency,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "est_cost_usd": cost,
        "cost_basis": "priced:fixture" if cost is not None else "unknown_model",
        "tool_calls": 3,
        "recovery_rounds": 1,
        "revision_rounds": 0,
        "evidence_count": len(evidence),
        "terminal_state": state,
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


def write_raw(directory: Path, records: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "raw.jsonl"
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


# --- Descriptive statistics ------------------------------------------------


def test_summarise_counts_present_values_and_missing_ones():
    stats = analyze.summarise([1.0, 2.0, 3.0, None])
    assert stats.n == 3
    assert stats.missing == 1
    assert stats.median == 2.0
    assert stats.minimum == 1.0 and stats.maximum == 3.0


def test_summarise_of_nothing_is_empty_not_zero():
    stats = analyze.summarise([None, None])
    assert stats.n == 0
    assert stats.median is None
    assert "no data" in stats.describe()


def test_percentile_is_interpolated():
    values = [10.0, 20.0, 30.0, 40.0]
    assert analyze.percentile(values, 0.0) == 10.0
    assert analyze.percentile(values, 1.0) == 40.0
    assert analyze.percentile(values, 0.5) == pytest.approx(25.0)
    with pytest.raises(ValueError):
        analyze.percentile([], 0.5)


def test_rate_returns_the_count_as_well_as_the_share():
    assert analyze.rate([True, False, True]) == (2, 3, pytest.approx(2 / 3))
    assert analyze.rate([]) == (0, 0, None)


# --- Loading ---------------------------------------------------------------


def test_missing_raw_jsonl_is_reported_not_raised(tmp_path):
    analysis = analyze.load_analysis(tmp_path / "absent", DATASET)
    assert analysis.records == []
    assert any("does not exist" in problem for problem in analysis.problems)


def test_a_torn_line_and_an_invalid_record_are_reported_not_scored(tmp_path):
    item = DATASET.by_id(GOOD)
    good = make_record(item, "sequential")
    (tmp_path / "raw.jsonl").write_text(
        json.dumps(good) + "\n" + "{not json" + "\n" + json.dumps({"run_key": "x"}) + "\n",
        encoding="utf-8",
    )
    analysis = analyze.load_analysis(tmp_path, DATASET)
    assert len(analysis.records) == 1
    assert len(analysis.scores) == 1
    assert any("not valid JSON" in problem for problem in analysis.problems)
    assert any("not a valid record" in problem for problem in analysis.problems)


def test_an_unknown_prompt_id_is_a_problem_not_a_score(tmp_path):
    item = DATASET.by_id(GOOD)
    orphan = make_record(item, "p2p", prompt_id="no-such-prompt")
    write_raw(tmp_path, [orphan])
    analysis = analyze.load_analysis(tmp_path, DATASET)
    assert len(analysis.records) == 1
    assert analysis.scores == []
    assert any("unknown prompt id" in problem for problem in analysis.problems)


# --- Scoring as the analyser uses it --------------------------------------


def test_a_fully_supported_record_is_scored_as_a_success():
    item = DATASET.by_id(GOOD)
    score = scoring.score_record(make_record(item, "sequential"), item)
    assert score.rubric_score == pytest.approx(1.0)
    assert score.citation_precision == pytest.approx(1.0)
    assert score.success is True
    assert score.failure_categories == []


def test_an_unsupported_record_is_not_a_success_and_is_labelled():
    item = DATASET.by_id(GOOD)
    score = scoring.score_record(make_record(item, "sequential", good=False), item)
    assert score.success is False
    assert "not_completed" in score.failure_categories
    assert "no_final_answer" in score.failure_categories


# --- Aggregation -----------------------------------------------------------


def _three_record_batch(tmp_path: Path) -> Path:
    batch = tmp_path / "batch-1"
    sequence = [
        make_record(DATASET.by_id(GOOD), "sequential", latency=10.0, cost=0.02),
        make_record(DATASET.by_id(RAG), "sequential", latency=20.0, cost=None),
        make_record(DATASET.by_id(CALC), "p2p", latency=30.0, cost=0.04),
    ]
    write_raw(batch, sequence)
    (batch / "run_meta.json").write_text(
        json.dumps(
            {
                "plan_id": "fixture-plan",
                "backend": "fixture",
                "model": "fixture-model",
                "started_at": "2025-01-01T00:00:00+00:00",
                "ended_at": "2025-01-01T00:20:00+00:00",
                "stopped_reason": None,
            }
        ),
        encoding="utf-8",
    )
    return batch


def test_pattern_aggregates_match_an_independent_recount(tmp_path):
    analysis = analyze.load_analysis(_three_record_batch(tmp_path), DATASET)
    rows = {row["pattern"]: row for row in analyze.pattern_rows(analysis)}

    assert rows["sequential"]["runs"] == 2
    assert rows["sequential"]["scored"] == 2
    assert rows["sequential"]["latency_n"] == 2
    assert rows["sequential"]["latency_median"] == pytest.approx(15.0)
    assert rows["p2p"]["runs"] == 1
    assert rows["p2p"]["latency_median"] == pytest.approx(30.0)
    # A run that could not be priced is counted as missing, never as zero cost.
    assert rows["sequential"]["cost_n"] == 1
    assert rows["sequential"]["cost_missing"] == 1
    assert rows["p2p"]["cost_median"] == pytest.approx(0.04)


def test_category_aggregates_cover_every_category_present(tmp_path):
    analysis = analyze.load_analysis(_three_record_batch(tmp_path), DATASET)
    rows = {row["category"]: row for row in analyze.category_rows(analysis)}
    assert set(rows) == {
        DATASET.by_id(GOOD).category,
        DATASET.by_id(RAG).category,
        DATASET.by_id(CALC).category,
    }
    assert sum(row["runs"] for row in rows.values()) == 3


def test_success_rate_comes_from_scored_records_only(tmp_path):
    batch = tmp_path / "batch"
    records = [
        make_record(DATASET.by_id(GOOD), "hierarchical"),
        make_record(DATASET.by_id(GOOD), "hierarchical", good=False, run_key="fail-1"),
    ]
    write_raw(batch, records)
    analysis = analyze.load_analysis(batch, DATASET)
    group = analyze.summarise_group(analyze.group_by_pattern(analysis)["hierarchical"])
    hits, total, share = group["success"]
    assert (hits, total) == (1, 2)
    assert share == pytest.approx(0.5)


def test_terminal_states_and_failures_are_counted_per_label(tmp_path):
    batch = tmp_path / "batch"
    records = [
        make_record(DATASET.by_id(GOOD), "p2p"),
        make_record(DATASET.by_id(GOOD), "p2p", good=False, run_key="fail-2"),
    ]
    write_raw(batch, records)
    analysis = analyze.load_analysis(batch, DATASET)
    assert analyze.terminal_state_counts(analysis) == {"completed": 1, "failed": 1}
    failures = analyze.failure_counts(analysis)
    assert failures.get("not_completed") == 1
    assert failures.get("no_citations") == 1


# --- Artefacts -------------------------------------------------------------


def test_analyse_writes_the_csv_set_jsonl_and_report(tmp_path):
    results = _three_record_batch(tmp_path)
    written = analyze.analyse(results, dataset=DATASET, charts=False)

    assert written["records"] == 3
    assert written["scored"] == 3
    for name in (
        "runs.csv",
        "summary_by_pattern.csv",
        "summary_by_category.csv",
        "failures.csv",
        "scored.jsonl",
        "REPORT.md",
    ):
        assert (results / name).is_file(), name

    header = (results / "runs.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == list(analyze.RUN_COLUMNS)
    assert len((results / "scored.jsonl").read_text(encoding="utf-8").splitlines()) == 3

    pattern_csv = (results / "summary_by_pattern.csv").read_text(encoding="utf-8")
    assert "sequential" in pattern_csv and "p2p" in pattern_csv


def test_report_names_the_patterns_and_carries_the_no_significance_note(tmp_path):
    results = _three_record_batch(tmp_path)
    analyze.analyse(results, dataset=DATASET, charts=False)
    report = (results / "REPORT.md").read_text(encoding="utf-8")

    assert "## By pattern" in report
    assert "sequential" in report and "p2p" in report
    assert analyze.NO_SIGNIFICANCE_NOTE in report
    assert "3" in report
    assert scoring.CRITIC_IS_GROUND_TRUTH is False
    assert "used to decide success" in report
    assert "CRITIC_IS_GROUND_TRUTH = False" in report


def test_empty_results_directory_says_so_instead_of_inventing_numbers(tmp_path):
    results = tmp_path / "empty"
    results.mkdir()
    (results / "raw.jsonl").write_text("", encoding="utf-8")
    written = analyze.analyse(results, dataset=DATASET, charts=False)

    assert written["records"] == 0
    report = (results / "REPORT.md").read_text(encoding="utf-8")
    assert "No runs recorded" in report
    assert "## By pattern" not in report
    assert "success rate" not in report.lower()


def test_charts_are_written_when_data_exists_and_skipped_when_it_does_not(tmp_path):
    results = _three_record_batch(tmp_path)
    analysis = analyze.load_analysis(results, DATASET)
    status = analyze.make_charts(analysis, results / "charts")

    assert status["latency_by_pattern.png"] == "written"
    assert status["tokens_by_pattern.png"] == "written"
    assert status["success_rate_by_pattern.png"] == "written"
    assert status["cost_by_pattern.png"] == "written"
    assert (results / "charts" / "latency_by_pattern.png").stat().st_size > 0

    # Now the same batch with every cost unpriced: the cost chart must be skipped,
    # and the gap must be named rather than drawn from zeroes.
    unpriced = tmp_path / "unpriced"
    write_raw(
        unpriced,
        [make_record(DATASET.by_id(GOOD), "sequential", cost=None)],
    )
    unpriced_analysis = analyze.load_analysis(unpriced, DATASET)
    unpriced_status = analyze.make_charts(unpriced_analysis, unpriced / "charts")
    assert unpriced_status["cost_by_pattern.png"].startswith("skipped")


def test_analyse_writes_nothing_outside_the_results_directory(tmp_path):
    results = _three_record_batch(tmp_path)
    before = {path.name for path in tmp_path.iterdir()}
    analyze.analyse(results, dataset=DATASET, charts=False)
    assert {path.name for path in tmp_path.iterdir()} == before


def test_cli_prints_a_summary_and_writes_the_report(tmp_path, capsys):
    results = _three_record_batch(tmp_path)
    code = analyze.main(["--results", str(results), "--no-charts"])
    output = capsys.readouterr().out

    assert code == 0
    assert "records scored : 3" in output
    assert (results / "REPORT.md").is_file()


def test_cli_rejects_a_results_directory_that_does_not_exist(tmp_path, capsys):
    code = analyze.main(["--results", str(tmp_path / "absent"), "--no-charts"])
    assert code == 2
    assert "does not exist" in capsys.readouterr().err
