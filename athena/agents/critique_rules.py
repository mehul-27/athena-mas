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

_QUALIFICATION = re.compile(
    r"\b(?:unverified|not verified|not established|no retrieved source|"
    r"limitation|remain(?:s)? unverified|cannot be (?:determined|verified))\b",
    re.I,
)

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


def calibrate(
    critique: Critique,
    report: FinalReport | None,
    assessment=None,
    evidence_texts: list[str] | None = None,
) -> Critique:
    """Raise an approving critique that the report itself contradicts.

    ``evidence_texts`` is optional. When it is supplied, claim-level over-claims
    (a superlative, a date, a specification, or a planned capability stated as
    existing) also block approval: approving prose that goes beyond the recorded
    evidence is the same failure as approving prose that contradicts it.
    """
    if report is None:
        return critique

    text = " ".join([report.answer, *report.key_points])
    qualified = bool(_QUALIFICATION.search(text))
    ceiling = getattr(assessment, "confidence_ceiling", None)
    qualified_possible = bool(getattr(assessment, "qualified_possible", False))

    reason = ""
    if ceiling is not None and (report.confidence or 0.0) > ceiling + 0.001:
        reason = (
            f"confidence {report.confidence:.2f} exceeds the evidence ceiling "
            f"{ceiling:.2f}"
        )
    elif (
        (report.confidence or 0.0) < LOW_CONFIDENCE
        and not (qualified and qualified_possible)
    ):
        reason = f"confidence {report.confidence:.2f} is below {LOW_CONFIDENCE}"
    elif says_unanswered(report.answer) or any(
        says_unanswered(point) for point in report.key_points
    ):
        reason = "the report says the evidence does not answer the question"

    overclaims = _overclaims(report, evidence_texts)
    if not reason and not overclaims:
        return critique

    issues = list(critique.issues)
    note = ISSUE if not reason.startswith("confidence") else f"{ISSUE} ({reason})"
    if reason and note not in issues:
        issues.append(note)
    for claim in overclaims:
        text_note = f"claim exceeds the recorded evidence: {claim}"
        if text_note not in issues:
            issues.append(text_note)
    if not issues:
        return critique
    severity = critique.severity if critique.severity in {"medium", "high"} else "medium"
    if overclaims:
        severity = "high"
    return critique.model_copy(
        update={"approved": False, "severity": severity, "issues": issues}
    )


def _overclaims(report: FinalReport, evidence_texts: list[str] | None) -> list[str]:
    """Report sentences that state more than every supporting source does."""
    if not evidence_texts:
        return []
    from athena.agents.claim_support import (
        capability_escalation,
        is_qualification,
        split_sentences,
        superlatives,
        support_score,
    )

    problems: list[str] = []
    for sentence in split_sentences(" ".join([report.answer, *report.key_points])):
        if is_qualification(sentence):
            continue
        supporting = [
            text for text in evidence_texts[:40]
            if support_score(sentence, text) >= 0.35
        ]
        if not supporting:
            continue
        claim_superlatives = [
            term
            for text in supporting
            for term in superlatives(sentence, text)
        ]
        if claim_superlatives and all(
            superlatives(sentence, text) for text in supporting
        ):
            problems.append(
                f"{sentence.strip()[:120]} (claims "
                f"{', '.join(dict.fromkeys(claim_superlatives))})"
            )
        elif all(capability_escalation(sentence, text) for text in supporting):
            problems.append(
                f"{sentence.strip()[:120]} (planned capability stated as existing)"
            )
    return list(dict.fromkeys(problems))[:5]

