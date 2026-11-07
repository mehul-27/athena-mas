"""Failure classification: infrastructure availability is not research quality.

The rule under test is narrow on purpose. A run with zero model calls is not
automatically an infrastructure failure - only a recorded infrastructure *cause*
makes it one. Everything else is a pipeline result.
"""

from __future__ import annotations

import json

import pytest

from evaluation import analyze, outcomes, schema, scoring


def _record(**overrides):
    """A minimal record shaped like one line of ``raw.jsonl``."""
    base = {
        "run_key": "x__hierarchical__r0",
        "prompt_id": "gf-01",
        "category": "general_factual",
        "difficulty": "easy",
        "persona": "novice",
        "pattern": "hierarchical",
        "context_sharing": "blackboard",
        "terminal_state": "failed",
        "model_calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "evidence_count": 0,
        "final_answer": None,
        "critic_status": "missing",
        "critic_approved": None,
        "errors": [],
        "stage_errors": [],
        "notes": [],
    }
    base.update(overrides)
    return base


TPD = (
    'RateLimitError: litellm.RateLimitError: RateLimitError: GroqException - '
    '{"error":{"message":"Rate limit reached for model `qwen/qwen3.8-27b` in '
    'organization `org_x` service tier `on_demand` on tokens per day (TPD): '
    'Limit 200000, Used 199534"}}'
)


# --- 1. quota before any model call ---------------------------------------


def test_quota_exhausted_before_the_first_model_call_is_infrastructure():
    record = _record(stage_errors=[TPD])
    result = outcomes.classify(record)

    assert result.is_infrastructure
    assert result.failure_code == "provider_quota"
    assert result.failure_phase == "before_model"
    assert outcomes.blocked_before_model(record)
    assert not outcomes.interrupted_after_model(record)
    # The provider's own words survive verbatim.
    assert "tokens per day" in result.failure_reason


# --- 2. quota after execution began ---------------------------------------


def test_quota_after_a_model_call_is_still_infrastructure_but_a_different_phase():
    record = _record(model_calls=1, tokens_in=692, tokens_out=108, stage_errors=[TPD])
    result = outcomes.classify(record)

    assert result.is_infrastructure
    assert result.failure_code == "provider_quota"
    assert result.failure_phase == "after_model"
    assert outcomes.model_started(record)
    assert outcomes.interrupted_after_model(record)
    # The two phases must not be conflated.
    assert not outcomes.blocked_before_model(record)


# --- 3. local limiter refusal ---------------------------------------------


def test_local_rate_limiter_refusal_is_infrastructure():
    record = _record(
        errors=[
            "RateLimitWaitError: would wait 60.0s for 1500 tokens "
            "(limit 1000/min, used 3000); max wait is 120s"
        ]
    )
    result = outcomes.classify(record)

    assert result.is_infrastructure
    assert result.failure_code == "local_rate_limit"


# --- 4. MCP startup failure ------------------------------------------------


def test_mcp_startup_failure_is_infrastructure():
    record = _record(
        stage_errors=[
            "Agent web_researcher will run without the tools from toolset "
            "MCPToolset, which failed to load: Failed to create MCP session: "
            "I/O operation on closed file"
        ]
    )
    result = outcomes.classify(record)

    assert result.is_infrastructure
    assert result.failure_code == "mcp_startup"


# --- 5. ordinary pipeline failure before any model call -------------------


def test_an_ordinary_pipeline_failure_with_no_model_call_is_not_infrastructure():
    """The rule that keeps the metric honest: no cause, no excuse."""
    record = _record(errors=["no final report was written"])
    result = outcomes.classify(record)

    assert result.failure_class == "pipeline"
    assert result.failure_code == "no_final_report"
    assert not result.is_infrastructure
    assert not outcomes.blocked_before_model(record)
    assert not outcomes.interrupted_after_model(record)


def test_zero_model_calls_alone_never_implies_infrastructure():
    assert not outcomes.classify(_record()).is_infrastructure
    assert not outcomes.classify(_record(errors=[])).is_infrastructure


# --- 6. completed but critic-rejected -------------------------------------


def test_completed_but_critic_rejected_is_a_pipeline_result():
    record = _record(
        terminal_state="completed",
        final_answer="An answer that was judged inadequate.",
        critic_status="rejected",
        critic_approved=False,
        critic_evaluable=True,
    )
    result = outcomes.classify(record)

    assert result.failure_class == "pipeline"
    assert result.failure_code == "critic_rejected"
    assert not result.is_infrastructure
    assert outcomes.critic_evaluable(record)


# --- 7. completed and approved --------------------------------------------


def test_completed_and_approved_has_no_failure():
    record = _record(
        terminal_state="completed",
        final_answer="Tungsten, atomic number 74.",
        critic_status="approved",
        critic_approved=True,
        evidence_count=7,
        citations=[{"source_type": "web", "source_ref": "https://example.org",
                    "quote_or_summary": "W is tungsten"}],
        citation_count=1,
    )
    result = outcomes.classify(record)

    assert result.failure_class == "none"
    assert result.failure_code == "none"
    assert not result.is_infrastructure
    assert outcomes.is_valid_completed(record)


# --- history must keep working --------------------------------------------


def test_a_historical_record_without_the_new_fields_still_classifies():
    """Records written before this feature must classify identically."""
    historical = {
        "run_key": "old",
        "terminal_state": "failed",
        "stage_errors": [TPD],
        # no failure_class / failure_code / failure_phase / citation_count
    }
    result = outcomes.classify(historical)
    assert result.failure_code == "provider_quota"
    assert result.failure_phase == "before_model"


def test_a_stamped_classification_wins_over_re_derivation():
    record = _record(
        errors=["something ambiguous"],
        failure_class="infrastructure",
        failure_code="provider_unavailable",
        failure_reason="the provider refused the connection",
        failure_phase="after_model",
    )
    result = outcomes.classify(record)
    assert result.failure_code == "provider_unavailable"
    assert result.failure_reason == "the provider refused the connection"


# --- citation count consistency -------------------------------------------


def test_missing_citation_count_is_read_from_the_list_not_treated_as_zero():
    record = {
        "run_key": "old",
        "citations": [{"source_ref": "a"}, {"source_ref": "b"}, {"source_ref": "c"}],
    }
    assert outcomes.citation_count(record) == 3


def test_a_stamped_citation_count_is_used_when_present():
    assert outcomes.citation_count({"citation_count": 5, "citations": []}) == 5


def test_no_citations_at_all_is_zero():
    assert outcomes.citation_count({}) == 0


# --- schema compatibility --------------------------------------------------


def test_the_schema_accepts_old_records_and_defaults_the_new_fields():
    payload = {
        "run_key": "old",
        "prompt_id": "gf-01",
        "category": "general_factual",
        "difficulty": "easy",
        "persona": "novice",
        "pattern": "hierarchical",
        "context_sharing": "blackboard",
        "terminal_state": "completed",
        # A completed record must carry an answer and a latency; those
        # invariants predate this change and are deliberately preserved.
        "final_answer": "An answer from a record written before classification existed.",
        "latency_s": 12.5,
    }
    record = schema.RunRecord.model_validate(payload)
    assert record.citation_count is None
    assert record.failure_class == ""
    assert record.failure_phase == ""


def test_the_schema_keeps_the_original_error_text():
    payload = {
        "run_key": "x",
        "prompt_id": "gf-01",
        "category": "general_factual",
        "difficulty": "easy",
        "persona": "novice",
        "pattern": "hierarchical",
        "context_sharing": "blackboard",
        "terminal_state": "infra_error",
        "failure_class": "infrastructure",
        "failure_code": "provider_quota",
        "failure_reason": TPD,
        "failure_phase": "before_model",
        "errors": [TPD],
    }
    record = schema.RunRecord.model_validate(payload)
    assert record.failure_reason == TPD
    assert record.errors == [TPD]
