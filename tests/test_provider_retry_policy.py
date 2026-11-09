"""Offline retry policy checks; injected generators never contact providers."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("CREWAI_TELEMETRY_DISABLED", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

import pytest
from pydantic import ValidationError

from athena.agents.crew_writer import write_report
from athena.blackboard import (
    Blackboard,
    EvidenceItem,
    FinalReport,
    SQLiteStore,
    Task,
)
from athena.config.ratelimit import RateLimitWaitError
from athena.observability.recorder import Recorder, set_recorder
from athena.observability.store import SQLiteEventStore
from evaluation import outcomes, runner, schema


REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_RECORD = {
    "run_key": "synthetic__hierarchical__r0",
    "prompt_id": "gf-01",
    "category": "general_factual",
    "difficulty": "easy",
    "persona": "novice",
    "pattern": "hierarchical",
    "context_sharing": "blackboard",
}
SECRET_CANARY = "gsk_testcanary0123456789abcdef"


@pytest.fixture()
def bb(tmp_path):
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(
        Task(question="What is systems engineering?"), store=store
    )
    blackboard.append(
        "evidence",
        EvidenceItem(
            id="doc_1",
            source_type="rag",
            source_ref="handbook.pdf#p11-chunk-28",
            content="NASA defines systems engineering as a methodical approach.",
            score=0.64,
            retrieved_by="researcher",
        ),
        author="researcher",
    )
    yield blackboard, store
    set_recorder(None)


def _report(answer: str = "Systems engineering is methodical.") -> FinalReport:
    return FinalReport(answer=answer, key_points=["A key point."], confidence=0.8,
                       run_id="synthetic")


def _retry_events(events: SQLiteEventStore):
    return [event for event in events.events("synthetic") if event.kind == "retry"]


def _record(**overrides):
    return schema.RunRecord.model_validate({**BASE_RECORD, **overrides}).model_dump(
        mode="json"
    )


def _batch_for_executor(tmp_path, executor):
    dataset = schema.load_dataset(REPO_ROOT / "evaluation" / "dataset.json")
    config = schema.load_config(REPO_ROOT / "evaluation" / "config.json")
    return runner.Runner(
        dataset, config, tmp_path, entries=config.entries()[:1], executor=executor
    )


def test_legacy_two_argument_executor_is_invoked_once(tmp_path):
    calls = []

    def legacy(entry, item):
        calls.append((entry.run_key, item.id))
        return {"result": "ok"}

    batch = _batch_for_executor(tmp_path, legacy)
    entry = batch.entries[0]
    item = batch.dataset.by_id(entry.prompt_id)
    assert batch._call_executor(entry, item, attempt=2, attempt_id="attempt-2") == {
        "result": "ok"
    }
    assert calls == [(entry.run_key, item.id)]


def test_internal_attempt_typeerror_does_not_invoke_executor_twice(tmp_path):
    calls = []

    def executor(entry, item, *, attempt, attempt_id):
        calls.append((entry.run_key, item.id, attempt, attempt_id))
        raise TypeError("internal attempt handling failed")

    batch = _batch_for_executor(tmp_path, executor)
    entry = batch.entries[0]
    item = batch.dataset.by_id(entry.prompt_id)
    with pytest.raises(TypeError, match="internal attempt"):
        batch._call_executor(entry, item, attempt=2, attempt_id="attempt-2")
    assert calls == [(entry.run_key, item.id, 2, "attempt-2")]


@pytest.mark.parametrize("critic_status, critic_approved", [
    (None, None),
    ("missing", False),
    ("failed", None),
    ("failed", False),
])
def test_confirmed_503_retries_when_critic_status_is_absent(
    critic_status, critic_approved
):
    record = _record(
        terminal_state="infra_error",
        model_calls=1,
        errors=["HTTP 503 Service Unavailable"],
        critic_status=critic_status,
        critic_approved=critic_approved,
    )
    assert outcomes.classify(record).failure_code == "provider_unavailable"
    assert runner.should_retry(record) is True


@pytest.mark.parametrize("record, expected", [
    (
        {"terminal_state": "error", "errors": ["ValueError: synthetic harness bug"]},
        False,
    ),
    (
        {"terminal_state": "error", "errors": ["APIError: HTTP 401 invalid API key"]},
        False,
    ),
    (
        {"terminal_state": "error", "errors": [
            "APIError: malformed payload at character 503"
        ]},
        False,
    ),
    (
        {"terminal_state": "error", "errors": ["ServiceUnavailableError"]},
        True,
    ),
    (
        {"terminal_state": "error", "errors": ["APIConnectionError: connection reset"]},
        True,
    ),
    (
        {"terminal_state": "error", "errors": ["HTTP 503 Service Unavailable; HTTP 401"]},
        False,
    ),
    (
        {"terminal_state": "error", "errors": [
            "RateLimitWaitError: would wait 61.0s for 500 tokens (limit 1000/min)"
        ]},
        False,
    ),
    (
        {"terminal_state": "error", "errors": [
            "RateLimitError: rate_limit_exceeded; provider quota exhausted"
        ]},
        False,
    ),
    (
        {"terminal_state": "timeout", "errors": []},
        True,
    ),
    (
        {"terminal_state": "timeout", "errors": ["HTTP 401 AuthenticationError timed out"]},
        False,
    ),
    (
        {
            "terminal_state": "infra_error",
            "errors": ["HTTP 503 Service Unavailable"],
            "critic_status": "rejected",
            "final_answer": None,
        },
        False,
    ),
    (
        {
            "terminal_state": "infra_error",
            "errors": ["HTTP 503 Service Unavailable"],
            "critic_status": "missing",
            "critic_approved": False,
            "final_answer": "A saved answer is already available.",
        },
        False,
    ),
])
def test_only_retryable_infrastructure_failures_are_retried(record, expected):
    validated = _record(**record)
    assert runner.should_retry(validated) is expected


def test_completed_critic_result_and_budget_stop_are_never_retried():
    completed = _record(
        terminal_state="completed",
        final_answer="A saved answer.",
        latency_s=1.0,
        critic_status="approved",
        critic_approved=True,
    )
    budget = _record(terminal_state="budget_stopped", errors=["budget cap reached"])
    assert runner.should_retry(completed) is False
    assert runner.should_retry(budget) is False


def test_ambiguous_default_false_without_critic_status_is_not_retried():
    record = _record(
        terminal_state="infra_error",
        errors=["HTTP 503 Service Unavailable"],
        critic_approved=False,
    )
    assert runner.should_retry(record) is False


def test_provider_and_local_limiter_exceptions_propagate_without_writer_retry(bb, tmp_path):
    blackboard, store = bb
    cases = [
        RuntimeError("HTTP 503 Service Unavailable"),
        RateLimitWaitError("would wait 60.0s for 100 tokens (limit 1000/min)"),
    ]
    for index, failure in enumerate(cases):
        events = SQLiteEventStore(tmp_path / f"events-{index}.db")
        set_recorder(Recorder("synthetic", events))
        calls = []

        def fail(_prompt):
            calls.append(True)
            raise failure

        with pytest.raises(type(failure)):
            write_report(
                blackboard.run_id, "write the report", store=store, generate=fail
            )
        assert calls == [True]
        assert _retry_events(events) == []
        assert Blackboard.open(blackboard.run_id, store).read("final_report") is None


def test_chained_provider_error_does_not_become_invalid_output_retry(bb, tmp_path):
    blackboard, store = bb
    try:
        FinalReport.model_validate({"answer": "invalid"})
    except ValidationError as validation_error:
        try:
            raise RuntimeError("HTTP 503 Service Unavailable") from validation_error
        except RuntimeError as provider_error:
            failure = provider_error

    events = SQLiteEventStore(tmp_path / "events.db")
    set_recorder(Recorder("synthetic", events))
    calls = []

    def fail(_prompt):
        calls.append(True)
        raise failure

    with pytest.raises(RuntimeError, match="503"):
        write_report(blackboard.run_id, "write", store=store, generate=fail)
    assert calls == [True]
    assert _retry_events(events) == []


def test_crewai_validation_wrapper_with_finalreport_cause_is_retried(bb, tmp_path):
    from crewai.utilities.converter import ConverterError

    blackboard, store = bb
    events = SQLiteEventStore(tmp_path / "events.db")
    set_recorder(Recorder("synthetic", events))
    calls = []

    def generate(_prompt):
        calls.append(True)
        if len(calls) == 1:
            try:
                FinalReport.model_validate({"answer": "incomplete"})
            except ValidationError as validation_error:
                raise ConverterError("failed to convert FinalReport") from validation_error
        return _report()

    write_report(blackboard.run_id, "write", store=store, generate=generate)
    assert calls == [True, True]
    assert [row.extra["retry_type"] for row in _retry_events(events)] == ["invalid_json"]


def test_invalid_structured_output_retries_once_and_redacts_canary(bb, tmp_path):
    blackboard, store = bb
    events = SQLiteEventStore(tmp_path / "events.db")
    set_recorder(Recorder("synthetic", events))
    calls = []

    def generate(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return (
                {"answer": "incomplete", SECRET_CANARY: "synthetic value"},
                {"tokens_in": 7, "tokens_out": 2, "calls": 1},
            )
        return (
            _report(),
            {"tokens_in": 5, "tokens_out": 3, "calls": 1},
        )

    ack = write_report(blackboard.run_id, "write", store=store, generate=generate)
    assert len(calls) == 2
    assert "FinalReport validation failed" in calls[1]
    assert SECRET_CANARY not in calls[1]
    assert ack["tokens_in"] == 12
    assert ack["tokens_out"] == 5
    assert ack["calls"] == 2
    rows = _retry_events(events)
    assert [row.extra["retry_type"] for row in rows] == ["invalid_json"]
    report, _ = blackboard.read("final_report")
    assert report.answer == "Systems engineering is methodical."
    assert SECRET_CANARY not in report.model_dump_json()


def test_invalid_output_falls_back_after_one_correction_without_persisting_canary(
    bb, tmp_path
):
    blackboard, store = bb
    events = SQLiteEventStore(tmp_path / "events.db")
    set_recorder(Recorder("synthetic", events))
    calls = []

    def invalid(prompt):
        calls.append(prompt)
        return {"answer": "incomplete", SECRET_CANARY: "synthetic value"}

    ack = write_report(blackboard.run_id, "write", store=store, generate=invalid)
    assert ack["fallback"] is True
    assert len(calls) == 2
    assert all(SECRET_CANARY not in prompt for prompt in calls)
    report, _ = blackboard.read("final_report")
    draft, _ = blackboard.read("draft")
    assert SECRET_CANARY not in report.model_dump_json()
    assert SECRET_CANARY not in draft.text
    assert SECRET_CANARY not in ack.get("note", "")
    assert [row.extra["retry_type"] for row in _retry_events(events)] == ["invalid_json"]
    assert all(SECRET_CANARY not in str(row.extra) for row in _retry_events(events))


def test_truncation_retry_parses_real_generate_tuple_and_propagates_provider_error(
    bb, tmp_path
):
    blackboard, store = bb
    events = SQLiteEventStore(tmp_path / "events.db")
    set_recorder(Recorder("synthetic", events))
    calls = []

    def complete_after_truncation(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return (_report("Cut off mid senten"), {"tokens_in": 4, "tokens_out": 2})
        return (_report("A complete answer."), {"tokens_in": 6, "tokens_out": 3})

    ack = write_report(
        blackboard.run_id, "write", store=store, generate=complete_after_truncation
    )
    assert len(calls) == 2
    assert ack["tokens_in"] == 10
    assert ack["tokens_out"] == 5
    assert [row.extra["retry_type"] for row in _retry_events(events)] == [
        "truncated_report"
    ]
    report, _ = blackboard.read("final_report")
    assert report.answer == "A complete answer."

    fresh = Blackboard.create(
        Task(question="What is systems engineering?"), store=store
    )
    fresh.append(
        "evidence",
        EvidenceItem(
            id="doc_2",
            source_type="rag",
            source_ref="handbook.pdf#p11-chunk-29",
            content="Systems engineering integrates disciplines.",
            score=0.64,
            retrieved_by="researcher",
        ),
        author="researcher",
    )
    failure_events = SQLiteEventStore(tmp_path / "events-failure.db")
    set_recorder(Recorder("synthetic", failure_events))
    failure_calls = []

    def fail_truncation_retry(prompt):
        failure_calls.append(prompt)
        if len(failure_calls) == 1:
            return _report("Cut off mid senten")
        raise RuntimeError("HTTP 503 Service Unavailable")

    with pytest.raises(RuntimeError, match="503"):
        write_report(
            fresh.run_id, "write", store=store, generate=fail_truncation_retry
        )
    assert len(failure_calls) == 2
    assert Blackboard.open(fresh.run_id, store).read("final_report") is None
    assert [row.extra["retry_type"] for row in _retry_events(failure_events)] == [
        "truncated_report"
    ]


def test_invalid_truncation_retry_preserves_attempt_usage(bb, tmp_path):
    blackboard, store = bb
    events = SQLiteEventStore(tmp_path / "events.db")
    set_recorder(Recorder("synthetic", events))
    calls = []

    def generate(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return (_report("Cut off mid senten"), {"tokens_in": 4, "tokens_out": 2})
        return (
            {"answer": "still invalid"},
            {"tokens_in": 6, "tokens_out": 3},
        )

    ack = write_report(blackboard.run_id, "write", store=store, generate=generate)
    assert len(calls) == 2
    assert ack["tokens_in"] == 10
    assert ack["tokens_out"] == 5
    assert ack["truncated"] is True
    assert [row.extra["retry_type"] for row in _retry_events(events)] == [
        "truncated_report"
    ]
    report, _ = blackboard.read("final_report")
    assert report.answer.endswith("[truncated]")
