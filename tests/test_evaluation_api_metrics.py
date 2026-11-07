"""The evaluation API exposes operational metrics without hiding failure.

The important contract: ``success_rate`` stays exactly as it was, the
operational block is added beside it, and an unmeasurable quality figure is
``null``/``measurable: false`` rather than a fabricated zero.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from athena.api.app import create_app
from evaluation import analyze, outcomes, schema, scoring

REPO = Path(__file__).resolve().parents[1]
BATCH = REPO / "evaluation" / "results" / "20261107T150000Z"

needs_batch = pytest.mark.skipif(
    not (BATCH / "raw.jsonl").exists(), reason="the historical batch is not present"
)


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


@needs_batch
def test_the_zero_success_rate_is_still_reported(client):
    """The old metric is preserved verbatim, not replaced."""
    body = client.get(f"/api/eval/summary?run={BATCH.name}").json()

    row = body["patterns"][0]
    assert row["successes"] == 0
    assert row["scored"] == 33
    assert row["success_rate"] == 0.0


@needs_batch
def test_the_operational_block_matches_the_investigation(client):
    body = client.get(f"/api/eval/summary?run={BATCH.name}").json()
    op = body["operational"]

    assert op["attempts"] == 33
    # 6 of 33 reached a model; all 33 were infrastructure-caused.
    assert op["model_start"] == {"hits": 6, "n": 33, "rate": 0.1818}
    assert op["infrastructure_failure"]["hits"] == 33
    assert op["infrastructure_blocked"] == {"hits": 27, "n": 33, "rate": 0.8182}
    assert op["interrupted_after_model"] == {"hits": 6, "n": 33, "rate": 0.1818}
    assert op["research_completion"] == {"hits": 0, "n": 33, "rate": 0.0}
    assert op["scoring_success"] == {"hits": 0, "n": 33, "rate": 0.0}


@needs_batch
def test_critic_approval_has_an_empty_denominator_not_a_fake_zero(client):
    body = client.get(f"/api/eval/summary?run={BATCH.name}").json()
    op = body["operational"]

    # No run ever reached the critic, so the rate is null - not 0%.
    assert op["critic_approval"] == {"hits": 0, "n": 0, "rate": None}
    assert op["critic_evaluable_runs"] == 0


@needs_batch
def test_research_quality_is_null_and_not_measurable(client):
    body = client.get(f"/api/eval/summary?run={BATCH.name}").json()
    quality = body["operational"]["research_quality"]

    assert quality["mean"] is None
    assert quality["n"] == 0
    assert quality["measurable"] is False
    assert "N/A" in quality["display"]
    assert body["operational"]["valid_completed_runs"] == 0


@needs_batch
def test_failure_causes_are_reported_without_rewriting_the_records(client):
    body = client.get(f"/api/eval/summary?run={BATCH.name}").json()
    causes = body["operational"]["failure_codes"]

    assert causes["provider_quota"] == 32
    assert causes["local_rate_limit"] == 1
    # Every terminal state is still the historical one: nothing was rewritten.
    assert body["operational"]["terminal_states"] == {"failed": 33}


@needs_batch
def test_a_batch_with_one_valid_run_reports_a_measured_quality(client):
    body = client.get("/api/eval/summary?run=20261107T133000Z").json()
    quality = body["operational"]["research_quality"]

    assert quality["measurable"] is True
    assert quality["n"] == 1
    assert quality["mean"] is not None
    assert body["operational"]["critic_evaluable_runs"] == 1


def _fake_record(**overrides):
    base = {
        "run_key": "k",
        "prompt_id": "gf-01",
        "category": "general_factual",
        "difficulty": "easy",
        "persona": "novice",
        "pattern": "hierarchical",
        "context_sharing": "blackboard",
        "terminal_state": "failed",
        "model_calls": 0,
        "errors": [],
        "stage_errors": [],
        "latency_s": 10.0,
    }
    base.update(overrides)
    return base


def test_the_pattern_row_carries_every_metric_and_keeps_success_rate():
    dataset = schema.load_dataset(REPO / "evaluation" / "dataset.json")
    analysis = analyze.Analysis(results_dir=Path("."))
    analysis.records = [
        _fake_record(
            run_key="a",
            stage_errors=['GroqException "on tokens per day (TPD): Limit 200000"'],
        )
    ]
    analysis.scores, _ = scoring.score_records(analysis.records, dataset)

    from athena.api.eval_routes import _pattern_rows

    row = _pattern_rows(analysis)[0]
    for key in (
        "success_rate",
        "model_start",
        "research_completion",
        "critic_approval",
        "critic_evaluable_runs",
        "scoring_success",
        "infrastructure_failure",
        "infrastructure_blocked",
        "interrupted_after_model",
        "valid_completed_runs",
        "research_quality_n",
        "research_quality_text",
        "quality_measured",
    ):
        assert key in row, key
    assert row["quality_measured"] is False
    assert row["research_quality_n"] == 0


def test_the_route_serialises_to_json():
    """The payload must be JSON-serialisable: it is a response body."""
    dataset = schema.load_dataset(REPO / "evaluation" / "dataset.json")
    analysis = analyze.Analysis(results_dir=Path("."))
    analysis.records = [_fake_record()]
    analysis.scores, _ = scoring.score_records(analysis.records, dataset)

    from athena.api.eval_routes import _operational_summary, _pattern_rows

    payload = {"patterns": _pattern_rows(analysis),
               "operational": _operational_summary(analysis)}
    text = json.dumps(payload)
    assert "research_quality" in text
    assert json.loads(text)["operational"]["research_quality"]["measurable"] is False
