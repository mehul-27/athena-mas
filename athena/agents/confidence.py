"""Completeness-aware confidence, recomputed over the final answer.

The writer's self-reported confidence is a guess. This module turns the
deterministic evidence assessment, the claim-level validation of the shipped
sentences and the critic's verdict into one ceiling, and lowers the report to it.

It is applied *after* sanitization and *after* the critic has reviewed the
report, so the number that ships describes the answer that ships: an answer
whose requested aspects, subject and citations were all established, or an
answer that names what it could not establish.

The recomputation is idempotent: a report already at or below the ceiling is
left untouched (no extra blackboard write, no version bump).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from athena.agents import bb_io
from athena.agents.claim_support import validate_claims
from athena.agents.sufficiency import Assessment, assess
from athena.blackboard import Blackboard, Critique, FinalReport
from athena.observability.recorder import get_recorder

# Ceilings applied by this module, on top of the evidence ceiling.
CEILING_CRITIC_HIGH = 0.5
CEILING_CRITIC_MEDIUM = 0.6
CEILING_UNSUPPORTED_CLAIM = 0.5
CEILING_OVERCLAIM = 0.55
CEILING_CITATION_MISMATCH = 0.6
CEILING_NO_CITATIONS = 0.4


@dataclass(frozen=True)
class ConfidenceDecision:
    """The confidence that fits the final answer, and why."""

    previous: float
    confidence: float
    ceiling: float
    factors: tuple[tuple[str, float], ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.confidence < self.previous - 1e-9

    def summary(self) -> str:
        parts = [f"confidence {self.confidence:.2f}"]
        if self.changed:
            parts.append(f"lowered from {self.previous:.2f}")
        parts.append(f"ceiling {self.ceiling:.2f}")
        if self.notes:
            parts.append("; ".join(self.notes))
        return ": ".join(parts[:2]) + (" (" + "; ".join(parts[2:]) + ")" if len(parts) > 2 else "")


def _claim_checks(report: FinalReport, items, question: str):
    return validate_claims(
        [report.answer, *report.key_points],
        items,
        question,
        report.citations,
    )


def compute_confidence(
    report: FinalReport,
    assessment: Assessment,
    critique: Critique | None = None,
    items=(),
    question: str = "",
) -> ConfidenceDecision:
    """The evidence, claim and review ceiling, applied to one report."""
    ceiling = float(assessment.confidence_ceiling)
    factors: list[tuple[str, float]] = [("evidence", ceiling)]
    notes: list[str] = []

    if not assessment.answer_as_asked:
        if assessment.missing_requirements:
            notes.append(
                "unestablished: " + "; ".join(assessment.missing_requirements)
            )
        if assessment.missing_aspects:
            notes.append("missing parts: " + ", ".join(assessment.missing_aspects))
        if assessment.missing_subjects:
            notes.append("missing subjects: " + ", ".join(assessment.missing_subjects))
        for conflict in assessment.conflicts:
            if conflict.unresolved:
                notes.append(conflict.note)

    if critique is not None and not critique.approved:
        critic_ceiling = (
            CEILING_CRITIC_HIGH if critique.severity == "high" else CEILING_CRITIC_MEDIUM
        )
        if critic_ceiling < ceiling:
            ceiling = critic_ceiling
            notes.append(
                f"critic rejected the report ({critique.severity} severity)"
            )
        factors.append(("critic", critic_ceiling))

    checks = _claim_checks(report, items, question) if items else ()
    unsupported = [check for check in checks if not check.supported]
    overclaims = [check for check in checks if check.exceeds or check.dropped_qualifier]
    mismatched = [check for check in checks if check.citation_mismatch]

    if unsupported:
        ceiling = min(ceiling, CEILING_UNSUPPORTED_CLAIM)
        factors.append(("claims", CEILING_UNSUPPORTED_CLAIM))
        notes.append(f"{len(unsupported)} claim(s) lack recorded support")
    if overclaims:
        ceiling = min(ceiling, CEILING_OVERCLAIM)
        factors.append(("overclaim", CEILING_OVERCLAIM))
        notes.append(f"{len(overclaims)} claim(s) exceed the recorded evidence")
    if mismatched:
        ceiling = min(ceiling, CEILING_CITATION_MISMATCH)
        factors.append(("citations", CEILING_CITATION_MISMATCH))
        notes.append(f"{len(mismatched)} claim(s) are not supported by a cited source")
    if checks and not report.citations:
        ceiling = min(ceiling, CEILING_NO_CITATIONS)
        factors.append(("no_citations", CEILING_NO_CITATIONS))
        notes.append("the report ships without citations")

    confidence = round(min(float(report.confidence or 0.0), ceiling), 3)
    return ConfidenceDecision(
        previous=float(report.confidence or 0.0),
        confidence=confidence,
        ceiling=round(ceiling, 3),
        factors=tuple(factors),
        notes=tuple(dict.fromkeys(notes)),
    )


def finalize_confidence(
    blackboard: Blackboard,
    run_id: str,
    author: str = "supervisor",
    emit: bool = True,
) -> ConfidenceDecision | None:
    """Recompute the shipped report's confidence; write it back when it drops."""
    report = bb_io.section(blackboard, "final_report")
    if report is None:
        return None
    task = bb_io.section(blackboard, "task")
    question = task.question if task else ""
    items = bb_io.load_evidence(blackboard, limit=None)
    from athena.agents.tool_report import build_tool_report

    assessment = assess(
        question,
        items,
        enabled_tools=task.tools if task else None,
        tool_report=build_tool_report(run_id, getattr(blackboard, "store", None)),
    )
    critique = bb_io.section(blackboard, "critique")
    decision = compute_confidence(report, assessment, critique, items, question)

    if decision.changed:
        blackboard.write(
            "final_report",
            report.model_copy(update={"confidence": decision.confidence}),
            author=author,
        )
        if emit:
            _emit(run_id, decision)
    return decision


def _emit(run_id: str, decision: ConfidenceDecision) -> None:
    recorder = get_recorder()
    if recorder is None:
        return
    recorder.emit(
        "confidence",
        "supervisor",
        "adk",
        payload_summary=decision.summary()[:400],
        extra={
            "previous": decision.previous,
            "confidence": decision.confidence,
            "ceiling": decision.ceiling,
            "factors": dict(decision.factors),
            "notes": list(decision.notes),
        },
    )


def confidence_notes(summary: Any) -> list[str]:
    """Read the notes recorded on a summary, for CLI output."""
    return list(getattr(summary, "confidence_notes", []) or [])
