"""Deterministic post-processing for the critic's verdict.

Low confidence and direct admissions that the core question is unanswered
block approval. Generic evidence-gap wording is only a blocker when coverage
assessment says the requested answer is incomplete, with the existing
qualified-partial exception preserved.
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
    "does not answer",
    "does not define",
    "does not contain",
    "not found in the evidence",
    "cannot be determined",
    "no explicit definition",
    "unable to determine",
    "cannot answer",
)
CORE_HINTS = ("question", "answer")
CONTEXT_HINTS = ("definition", "evidence", "handbook")
LOW_CONFIDENCE = 0.5
ISSUE = "report does not answer the core question"


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+", text or "") if part.strip()]


def says_unanswered(
    text: str,
    answer_as_asked: bool | None = None,
    qualified_partial: bool = False,
) -> bool:
    """A direct admission or an evidence gap when the task lacks coverage."""
    for sentence in _sentences(text):
        lowered = sentence.lower()
        if not any(phrase in lowered for phrase in UNANSWERED_PHRASES):
            continue
        if any(hint in lowered for hint in CORE_HINTS):
            return True
        if answer_as_asked is not True and not qualified_partial and any(
            hint in lowered for hint in CONTEXT_HINTS
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
    elif any(
        says_unanswered(
            part,
            getattr(assessment, "answer_as_asked", None),
            qualified_partial=qualified
            and bool(getattr(assessment, "qualified_possible", False)),
        )
        for part in (report.answer, *report.key_points)
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

