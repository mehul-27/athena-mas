"""The six evaluation metrics, and the undefined-versus-zero rule.

Research quality is only measurable over runs that actually produced research.
When none did, the mean is undefined - never 0, which would report an
availability outage as a research defect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation import analyze, outcomes, schema, scoring


def _stats(records, dataset=None):
    """Summarise a group the way the dashboard does."""
    dataset = dataset or schema.EvalDataset(items=[])
    scores, _ = scoring.score_records(records, dataset)
    pairs = []
    by_key = {score.run_key: score for score in scores}
    for record in records:
        pairs.append((record, by_key.get(record["run_key"])))
    return analyze.summarise_group(pairs)


REPO = Path(__file__).resolve().parents[1]


def _dataset(*items):
    """Real dataset items, so the fixture cannot drift from the real schema."""
    loaded = schema.load_dataset(REPO / "evaluation" / "dataset.json")
    if not items:
        return loaded
    wanted = {item["id"] for item in items}
    return schema.EvalDataset(
        items=[item for item in loaded.items if item.id in wanted]
    )


TPD = 'RateLimitError: GroqException - "on tokens per day (TPD): Limit 200000"'


def _infra_record(key="a"):
    return {
        "run_key": key,
        "prompt_id": "gf-01",
        "category": "general_factual",
        "difficulty": "easy",
        "persona": "novice",
        "pattern": "hierarchical",
        "context_sharing": "blackboard",
        "terminal_state": "failed",
        "model_calls": 0,
        "stage_errors": [TPD],
        "errors": [],
        "final_answer": None,
        "latency_s": 60.0,
    }


def _good_record(key="b"):
    return {
        "run_key": key,
        "prompt_id": "gf-01",
        "category": "general_factual",
        "difficulty": "easy",
        "persona": "novice",
        "pattern": "hierarchical",
        "context_sharing": "blackboard",
        "terminal_state": "completed",
        "model_calls": 5,
        "tokens_in": 100,
        "tokens_out": 20,
        "final_answer": "The element with symbol W is tungsten.",
        "critic_status": "approved",
        "critic_approved": True,
        "latency_s": 30.0,
        "evidence_count": 3,
        "citations": [
            {"source_type": "web", "source_ref": "https://example.org/w",
             "quote_or_summary": "Tungsten W"},
        ],
    }


# --- the six metrics ------------------------------------------------------


def test_the_six_metrics_are_all_present():
    stats = _stats([_infra_record(), _good_record()], _dataset())
    for key in (
        "model_start",
        "research_completion",
        "critic_evaluable_approval",
        "scoring_success",
        "infrastructure_failure",
        "research_quality",
    ):
        assert key in stats, key


def test_model_start_rate_counts_runs_that_reached_a_model():
    stats = _stats([_infra_record("a"), _good_record("b")], _dataset())
    hits, total, share = stats["model_start"]

    assert (hits, total) == (1, 2)
    assert share == pytest.approx(0.5)


def test_infrastructure_and_research_rates_are_reported_separately():
    stats = _stats([_infra_record("a"), _good_record("b")], _dataset())

    # Both describe the same two runs, but from opposite ends: one is a fault,
    # the other is an achievement, and they are never combined into one rate.
    assert stats["infrastructure_failure"][:2] == (1, 2)
    assert stats["infrastructure_blocked"][:2] == (1, 2)
    assert stats["attempts"] == 2
    assert "infrastructure_failure" in stats and "research_completion" in stats


def test_critic_approval_denominator_is_critic_evaluable_runs_not_all_runs():
    rejected = _good_record("c")
    rejected["critic_status"] = "rejected"
    rejected["critic_approved"] = False
    stats = _stats([_infra_record("a"), _good_record("b"), rejected], _dataset())

    hits, total, _ = stats["critic_evaluable_approval"]
    # Only two runs ever reached the critic; the infra run did not.
    assert total == 2
    assert hits == 1
    assert stats["critic_evaluable"][:2] == (2, 3)


# --- undefined versus zero ------------------------------------------------


def test_quality_is_undefined_when_no_run_completed():
    stats = _stats([_infra_record("a"), _infra_record("b")], _dataset())

    assert stats["research_quality"].n == 0
    assert stats["research_quality"].mean is None
    assert analyze.quality_mean_text(stats["research_quality"]) == analyze.QUALITY_UNDEFINED
    assert stats["valid_completed"] [0] == 0


def test_quality_is_undefined_not_zero_in_the_exported_row():
    rows = analyze.pattern_rows(
        _analysis([_infra_record("a"), _infra_record("b")], _dataset())
    )
    row = rows[0]

    assert row["research_quality_n"] == 0
    assert row["research_quality_mean"] == analyze.QUALITY_UNDEFINED
    assert row["research_quality_mean"] != "0"
    assert row["research_quality_mean"] != 0
    # The operational zero stays a zero.
    assert row["success_rate"] == "0/2 (0%)"


def test_a_genuine_zero_from_a_completed_run_is_reported_as_zero():
    """A completed run that truly scores zero must show 0, not N/A.

    Built directly so the test pins *this* rule rather than the rubric's own
    scoring, which is covered by the scoring tests.
    """
    measured_zero = analyze.Stats(n=1, missing=0, mean=0.0, median=0.0,
                                  p25=0.0, p75=0.0, minimum=0.0, maximum=0.0)
    text = analyze.quality_mean_text(measured_zero)

    assert text == "0.000"
    assert text != analyze.QUALITY_UNDEFINED
    assert "N/A" not in text


def test_quality_counts_only_valid_completed_runs():
    stats = _stats([_infra_record("a"), _good_record("b")], _dataset())

    # Exactly one run is eligible for the quality sample; the infrastructure
    # failure is excluded rather than averaged in as a zero.
    assert stats["valid_completed"][0] == 1
    assert stats["research_quality"].n == 1
    assert stats["runs"] == 2


def test_quality_sample_excludes_every_infrastructure_run():
    stats = _stats(
        [_infra_record("a"), _infra_record("b"), _infra_record("c")], _dataset()
    )
    assert stats["valid_completed"][0] == 0
    assert stats["research_quality"].n == 0
    assert stats["infrastructure_failure"][:2] == (3, 3)


# --- helper ---------------------------------------------------------------


def _analysis(records, dataset):
    analysis = analyze.Analysis(results_dir=Path("."))
    analysis.records = [dict(record) for record in records]
    analysis.scores, _ = scoring.score_records(analysis.records, dataset)
    return analysis
