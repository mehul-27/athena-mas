"""Critic calibration: a report that admits failure must not be approved."""

from __future__ import annotations

from athena.agents.critique_rules import ISSUE, calibrate, says_unanswered
from athena.blackboard import Critique, FinalReport

APPROVING = Critique(issues=[], severity="low", approved=True)


def _report(answer: str, confidence: float = 0.8, points: list | None = None) -> FinalReport:
    return FinalReport(answer=answer, key_points=points or [], confidence=confidence,
                       run_id="r")


def test_low_confidence_is_rejected_at_medium() -> None:
    result = calibrate(APPROVING, _report("A complete answer.", confidence=0.3))

    assert result.approved is False
    assert result.severity == "medium"
    assert any("confidence" in issue for issue in result.issues)


def test_an_unanswered_question_is_rejected() -> None:
    report = _report("The evidence does not state the definition of the term.")

    result = calibrate(APPROVING, report)

    assert result.approved is False
    assert result.severity == "medium"
    assert ISSUE in result.issues


def test_a_key_point_can_carry_the_admission() -> None:
    report = _report("A tidy answer.", points=["No explicit definition appears in the evidence."])

    assert calibrate(APPROVING, report).approved is False


def test_high_severity_is_preserved() -> None:
    strict = Critique(issues=["unsupported claim"], severity="high", approved=False)

    result = calibrate(strict, _report("x", confidence=0.1))

    assert result.severity == "high"
    assert "unsupported claim" in result.issues


def test_a_good_report_is_left_alone() -> None:
    result = calibrate(APPROVING, _report("Systems engineering is interdisciplinary."))

    assert result.approved is True
    assert result.severity == "low"
    assert result.issues == []


def test_a_caveat_about_a_detail_does_not_trigger() -> None:
    report = _report(
        "The handbook defines the process clearly. "
        "The appendix does not state the publication date."
    )

    assert calibrate(APPROVING, report).approved is True


def test_phrase_without_a_core_hint_does_not_trigger() -> None:
    assert says_unanswered("The appendix does not state the publication date.") is False
    assert says_unanswered("The evidence does not state the answer.") is True


def test_a_missing_report_is_left_alone() -> None:
    assert calibrate(APPROVING, None).approved is True


def test_the_revision_trigger_stays_high_only() -> None:
    """Calibration must not cause a revision: the loop only reacts to 'high'."""
    result = calibrate(APPROVING, _report("x", confidence=0.2))
    assert result.severity == "medium"
