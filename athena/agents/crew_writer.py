"""The writer role: turn task + evidence into a Draft and a FinalReport.

The model writes the prose; ``sanitize_report`` decides what counts as a source
and which tools ran. Generation is retried once with the validation error, and
then falls back to a minimal valid report so the pipeline always completes.
"""

from __future__ import annotations

from typing import Callable

from athena.agents import bb_io
from athena.agents.crew_roles import _crew_call, _unpack
from athena.agents.report_sanitize import sanitize_report
from athena.blackboard import Blackboard, BlackboardStore, Draft, FinalReport

Generate = Callable[[str], object]

MAX_ANSWER_WORDS = 150
TERMINAL = (".", "!", "?", '"', "'", "”", "’", ")")
SHORTER = (
    "\n\nYour previous answer was cut off mid-sentence. Rewrite the whole "
    f"report with an answer of at most 120 words that ends with a full stop."
)


def looks_truncated(answer: str) -> bool:
    """True when the answer did not finish on a sentence boundary."""
    text = (answer or "").strip()
    if not text:
        return True
    return not text.endswith(TERMINAL)


def _truncate(report: FinalReport) -> FinalReport:
    """Mark it, and stop claiming confidence we cannot justify."""
    return report.model_copy(
        update={
            "answer": report.answer.rstrip() + " [truncated]",
            "confidence": min(report.confidence, 0.5),
        }
    )


def _generate_report(prompt: str) -> tuple[object, dict]:
    return _crew_call("writer", "Write a cited research report.", prompt, FinalReport)


def _fallback_report(run_id: str, exc: Exception) -> FinalReport:
    """A legal FinalReport so the pipeline finishes instead of crashing."""
    return FinalReport(
        answer=(
            "The report could not be generated: the writer produced output that "
            f"failed validation ({type(exc).__name__}). The evidence remains on the "
            "blackboard for inspection."
        ),
        key_points=[],
        citations=[],
        confidence=0.0,
        tools_used=[],
        guardrail_flags=[],
        run_id=run_id,
    )


def writer_prompt(blackboard: Blackboard, instruction: str) -> str:
    critique = bb_io.critique_text(blackboard)
    revision = (
        f"\n\nPREVIOUS CRITIQUE (fix every listed issue):\n{critique}\n"
        if critique
        else ""
    )
    return (
        f"{instruction}\n\nTASK:\n{bb_io.task_text(blackboard)}\n\n"
        f"PLAN:\n{bb_io.plan_text(blackboard)}\n\n"
        f"EVIDENCE (cite only these source_ref values, exactly as written):\n"
        f"{bb_io.evidence_text(blackboard, limit=8)}\n\n"
        f"ANALYSIS:\n{bb_io.analysis_text(blackboard)}\n"
        f"{revision}\n"
        f"Write an answer of at most {MAX_ANSWER_WORDS} words that ends with a "
        "full stop, 3-5 key points, and 1-3 citations whose source_ref is "
        "copied verbatim from the evidence lines above. confidence is 0 to 1."
    )


def write_report(
    run_id: str,
    instruction: str,
    store: BlackboardStore | None = None,
    generate: Generate | None = None,
) -> dict:
    """Write ``draft`` and ``final_report`` as author ``writer``."""
    blackboard = Blackboard.open(run_id, store)
    prompt = writer_prompt(blackboard, instruction)
    generate = generate or _generate_report

    fallback = False
    stats: dict = {}
    try:
        report, stats = _unpack(generate(prompt))
    except Exception as exc:  # noqa: BLE001 - one bad generation must not stop the run
        try:
            report, stats = _unpack(
                generate(
                    f"{prompt}\n\nYour previous output was rejected: {exc}\n"
                    "Return only valid JSON."
                )
            )
        except Exception as exc2:  # noqa: BLE001 - documented fallback
            report = _fallback_report(run_id, exc2)
            fallback = True

    if not isinstance(report, FinalReport):
        report = FinalReport.model_validate(report)

    # A cut-off answer is a real defect: retry once, then mark it.
    truncated = False
    if looks_truncated(report.answer):
        try:
            retry = generate(prompt + SHORTER)
            if not isinstance(retry, FinalReport):
                retry = FinalReport.model_validate(retry)
            report = retry
        except Exception:  # noqa: BLE001 - keep the first answer rather than fail
            pass
        if looks_truncated(report.answer):
            report = _truncate(report)
            truncated = True

    report = sanitize_report(report, blackboard, run_id)

    cited = {citation.source_ref for citation in report.citations}
    evidence_ids = [
        item.id
        for ref, item in bb_io.evidence_by_ref(blackboard).items()
        if ref in cited
    ]
    blackboard.write(
        "draft",
        Draft(text=report.answer, based_on_evidence_ids=evidence_ids),
        author="writer",
    )
    version = blackboard.write("final_report", report, author="writer")

    ack = {"section": "final_report", "version": version, **stats}
    if fallback:
        ack["fallback"] = True
        ack["note"] = "writer produced invalid output twice; wrote a minimal report"
    if truncated:
        ack["truncated"] = True
    return ack
