"""Deterministic post-processing for the critic's verdict.

A model can approve a report that plainly admits it could not answer the
question. These rules make that impossible to ship: if the report has low
confidence, or says in so many words that the evidence does not cover the core
question, the critique is rejected with at least medium severity.

The phrase rule is deliberately conservative - it needs a hedge phrase *and* a
word pointing at the core question in the same sentence - so ordinary caveats
about details ("the appendix does not state the publication date") do not trip
it. It cannot understand context, so a sentence that happens to combine both is
a false positive; the revision loop only reacts to severity "high", so the cost
of a false positive is a medium-severity note, not another revision.
"""

from __future__ import annotations

import re

from athena.blackboard import Critique, FinalReport

UNANSWERED_PHRASES = (
    "does not state",
    "does not define",
    "does not contain",
    "not found in the evidence",
    "cannot be determined",
    "no explicit definition",
    "unable to determine",
)
CORE_HINTS = ("question", "answer", "definition", "evidence", "handbook")
LOW_CONFIDENCE = 0.5
ISSUE = "report does not answer the core question"


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+", text or "") if part.strip()]


def says_unanswered(text: str) -> bool:
    """A hedge phrase about the core question, in the same sentence."""
    for sentence in _sentences(text):
        lowered = sentence.lower()
        if any(phrase in lowered for phrase in UNANSWERED_PHRASES) and any(
            hint in lowered for hint in CORE_HINTS
        ):
            return True
    return False


def calibrate(critique: Critique, report: FinalReport | None) -> Critique:
    """Raise an approving critique that the report itself contradicts."""
    if report is None:
        return critique

    reason = ""
    if (report.confidence or 0.0) < LOW_CONFIDENCE:
        reason = f"confidence {report.confidence:.2f} is below {LOW_CONFIDENCE}"
    elif says_unanswered(report.answer) or any(
        says_unanswered(point) for point in report.key_points
    ):
        reason = "the report says the evidence does not answer the question"

    if not reason:
        return critique

    issues = list(critique.issues)
    note = ISSUE if not reason.startswith("confidence") else f"{ISSUE} ({reason})"
    if note not in issues:
        issues.append(note)
    severity = critique.severity if critique.severity in {"medium", "high"} else "medium"
    return critique.model_copy(
        update={"approved": False, "severity": severity, "issues": issues}
    )
