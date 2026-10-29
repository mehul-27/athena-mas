"""Critic diagnostics must distinguish rejection, failure, and missing critique."""

from __future__ import annotations

from athena.blackboard import Critique
from athena.summary import RunSummary, note_critique, set_outcome


def test_high_severity_rejection_carries_its_reason() -> None:
    summary = RunSummary(pattern="sequential")
    critique = Critique(
        issues=["finding 2 has no distinct primary source", "impact is unsupported"],
        severity="high",
        approved=False,
    )

    note_critique(summary, critique)

    assert summary.approved is False
    assert summary.severity == "high"
    assert summary.critic_status == "rejected"
    assert "finding 2" in summary.critique_reason


def test_critic_execution_failure_is_not_a_missing_approval() -> None:
    summary = RunSummary(pattern="sequential")
    summary.stage_errors.append("critic: IncompleteOutputException")

    note_critique(summary, None)

    assert summary.approved is None
    assert summary.critic_status == "failed"
    assert "critic failed" in summary.critique_reason


def test_missing_critique_is_explicitly_not_approval() -> None:
    summary = RunSummary(pattern="sequential")

    note_critique(summary, None)

    assert summary.approved is None
    assert summary.critic_status == "missing"
    assert "not an approval" in summary.critique_reason


def test_approved_critique_is_not_reported_as_rejected() -> None:
    summary = RunSummary(pattern="sequential")
    critique = Critique(issues=[], severity="low", approved=True)

    note_critique(summary, critique)

    assert summary.approved is True
    assert summary.critic_status == "approved"
    assert "approved" in summary.critique_reason


def test_terminal_outcome_does_not_call_rejection_success() -> None:
    rejected = RunSummary(pattern="sequential")
    rejected.critic_status = "rejected"
    rejected.approved = False
    set_outcome(rejected, report_present=True, evidence_sufficient=True)
    assert rejected.outcome == "rejected"
    assert rejected.degraded is True

    insufficient = RunSummary(pattern="sequential")
    insufficient.critic_status = "approved"
    insufficient.approved = True
    set_outcome(insufficient, report_present=True, evidence_sufficient=False)
    assert insufficient.outcome == "incomplete"
    assert insufficient.degraded is True

    approved = RunSummary(pattern="sequential")
    approved.critic_status = "approved"
    approved.approved = True
    set_outcome(approved, report_present=True, evidence_sufficient=True)
    assert approved.outcome == "approved"
    assert approved.degraded is False

    failed = RunSummary(pattern="sequential")
    failed.failed = True
    set_outcome(failed, report_present=False, evidence_sufficient=False)
    assert failed.outcome == "failed"
