"""The writer role: turn task + evidence into a Draft and a FinalReport.

The model writes the prose; ``sanitize_report`` decides what counts as a source
and which tools ran. Generation is retried once with the validation error, and
then falls back to a minimal valid report so the pipeline always completes.
"""

from __future__ import annotations

from typing import Callable

from athena.agents import bb_io
from athena.agents.crew_roles import crew_call, unpack_result
from athena.agents.report_sanitize import sanitize_report
from athena.agents.requirements import finding_requirement
from athena.agents.routing import request_profile
from athena.agents.sufficiency import (
    assess,
    coverage_text,
    gap_answer,
    partial_snippet_evidence,
)
from athena.agents.tool_report import build_tool_report, format_tool_status
from athena.agents.untrusted import UNTRUSTED_DATA_RULE
from athena.blackboard import Blackboard, BlackboardStore, Draft, FinalReport
from athena.observability.retry_events import (
    retry_invalid_output,
    retry_truncated_report,
)

Generate = Callable[[str], object]

MAX_ANSWER_WORDS = 150
TERMINAL = (".", "!", "?", '"', "'", "”", "’", ")")
SHORTER = (
    "\n\nYour previous answer was cut off mid-sentence. Rewrite the whole "
    "report with an answer of at most 120 words that ends with a full stop."
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
    return crew_call("writer", "Write a cited research report.", prompt, FinalReport)


def _fallback_report(run_id: str, exc: Exception) -> FinalReport:
    """A legal FinalReport so the pipeline finishes instead of crashing."""
    short = " ".join(str(exc).split())[:160]
    return FinalReport(
        answer=(
            f"The report could not be generated: writer failed with "
            f"{type(exc).__name__}: {short}. The evidence remains on the "
            "blackboard for inspection."
        ),
        key_points=[],
        citations=[],
        confidence=0.0,
        tools_used=[],
        guardrail_flags=[],
        run_id=run_id,
    )


def writer_prompt(
    blackboard: Blackboard,
    instruction: str,
    limitations: list[str] | tuple[str, ...] | None = None,
    blocking_issues: list[str] | tuple[str, ...] | None = None,
    partial: bool = False,
    coverage: str = "",
) -> str:
    critique = bb_io.critique_text(blackboard)
    revision = (
        f"\n\nPREVIOUS CRITIQUE (fix every listed issue):\n{critique}\n"
        if critique
        else ""
    )
    tool_status = ""
    run_id = getattr(blackboard, "run_id", "")
    if run_id:
        tool_status = format_tool_status(
            build_tool_report(run_id, getattr(blackboard, "store", None)),
            limitations=limitations,
        )
    limitation_block = ""
    if limitations:
        limitation_block = (
            "NON-BLOCKING EVIDENCE LIMITATIONS (state them; do not claim they were "
            "resolved):\n"
            + "\n".join(f"- {item}" for item in limitations)
            + "\n\n"
        )
    blocking_block = ""
    if blocking_issues:
        blocking_block = (
            "UNRESOLVED EVIDENCE REQUIREMENTS (do not make claims that depend on them):\n"
            + "\n".join(f"- {item}" for item in blocking_issues)
            + "\n\n"
        )
    partial_block = ""
    if partial:
        partial_block = (
            "PARTIAL ANSWER MODE: Only search-result snippets are available. Do not "
            "claim that any page was read or fetched. Answer the general principle "
            "only where the snippets directly state it, do not invent SpaceX-specific "
            "hardware details, and explicitly say which details remain unverified. "
            "Do not return a total-refusal answer when the snippets support the "
            "general principle. Use confidence 0.5 for this qualified partial answer. "
            "You may list snippet source_refs as citations, but describe them as "
            "search snippets, not fetched pages.\n\n"
        )
    coverage_block = f"EVIDENCE COVERAGE:\n{coverage}\n\n" if coverage else ""
    profile = request_profile(bb_io.task_text(blackboard))
    qualification_block = ""
    if profile.notes:
        qualification_block = (
            "REQUEST-SPECIFIC QUALIFICATION:\n"
            + "\n".join(f"- {note}" for note in profile.notes)
            + "\n\n"
        )
    status_block = (
        f"TOOL AND EVIDENCE STATUS:\n{tool_status}\n\n" if tool_status else ""
    )
    return (
        f"{instruction}\n\nTASK:\n{bb_io.task_text(blackboard)}\n\n"
        f"PLAN:\n{bb_io.plan_text(blackboard)}\n\n"
        f"{status_block}"
        f"{limitation_block}"
        f"{blocking_block}"
        f"{partial_block}"
        f"{coverage_block}"
        f"{qualification_block}"
        f"EVIDENCE (cite only these source_ref values, exactly as written):\n"
        f"{bb_io.evidence_text(blackboard, limit=8)}\n\n"
        f"ANALYSIS:\n{bb_io.analysis_text(blackboard)}\n"
        f"{revision}\n"
        f"{UNTRUSTED_DATA_RULE}\n"
        f"{finding_requirement(bb_io.task_text(blackboard))}\n"
        f"{_preferences_block()}"
        f"Write an answer of at most {MAX_ANSWER_WORDS} words that ends with a "
        "full stop, 3-5 key points, and 1-3 citations whose source_ref is "
        "copied verbatim from the evidence lines above. confidence is 0 to 1. "
        "Do not claim a tool was used unless the tool status shows a real "
        "invocation; describe an unmet requirement instead of filling it in. "
        "If usable evidence exists, answer from it and clearly qualify any "
        "non-blocking limitation. Never say that no external evidence was "
        "collected when the evidence section contains successfully fetched pages. "
        "In partial-answer mode, never claim a page was read; snippets remain snippets. "
        "Do not make numeric, performance, or hardware-specific claims from a search "
        "snippet; use fetched page text, or omit the claim. Keep every qualifier the "
        "source uses (for example 'about', 'up to', 'planned') and never add a "
        "superlative ('the first', 'the most efficient'), a patch number, a date or a "
        "specification the evidence does not state. When the general mechanism "
        "is supported but organization-specific hardware details remain unverified, "
        "state that limitation explicitly and do not exceed the evidence-coverage "
        "confidence ceiling."
    )


def write_report(
    run_id: str,
    instruction: str,
    store: BlackboardStore | None = None,
    generate: Generate | None = None,
) -> dict:
    """Write ``draft`` and ``final_report`` as author ``writer``."""
    blackboard = Blackboard.open(run_id, store)
    task = bb_io.section(blackboard, "task")
    question = task.question if task else ""
    report_tools = build_tool_report(run_id, store)
    evidence_items = bb_io.load_evidence(blackboard, limit=None)
    assessment = assess(
        question,
        evidence_items,
        enabled_tools=task.tools if task else None,
        tool_report=report_tools,
    )
    partial = partial_snippet_evidence(question, evidence_items, assessment)
    if not assessment.ok and not partial:
        report = FinalReport(
            answer=gap_answer(question, assessment),
            key_points=list(assessment.issues),
            citations=[],
            confidence=0.0,
            tools_used=[],
            guardrail_flags=[],
            run_id=run_id,
        )
        blackboard.write(
            "draft",
            Draft(text=report.answer, based_on_evidence_ids=[]),
            author="writer",
        )
        version = blackboard.write("final_report", report, author="writer")
        return {
            "section": "final_report",
            "version": version,
            "insufficient": True,
            "evidence_issues": list(assessment.issues),
        }

    prompt = writer_prompt(
        blackboard,
        instruction,
        assessment.limitations,
        assessment.issues if partial else None,
        partial=partial,
        coverage=coverage_text(assessment),
    )
    generate = generate or _generate_report

    fallback = False
    stats: dict = {}
    failure: Exception | None = None
    problems: list[str] = []
    try:
        report, stats = unpack_result(generate(prompt))
    except Exception as exc:  # noqa: BLE001 - one bad generation must not stop the run
        try:
            # The second call below is a real retry of the same work, so it is
            # reported as one (and only one) ``retry`` event before it runs.
            retry_invalid_output(exc, agent="writer")
            report, stats = unpack_result(
                generate(
                    f"{prompt}\n\nYour previous output was rejected: {exc}\n"
                    "Return only valid JSON."
                )
            )
        except Exception as exc2:  # noqa: BLE001 - documented fallback
            report = _fallback_report(run_id, exc2)
            fallback = True
            failure = exc2

    if not isinstance(report, FinalReport):
        report = FinalReport.model_validate(report)

    # A cut-off answer is a real defect: retry once, then mark it.
    truncated = False
    if looks_truncated(report.answer):
        # One re-generation is about to happen, so it is one ``retry`` event —
        # emitted here, in the branch that decided to retry, and only here.
        retry_truncated_report(agent="writer")
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

    report = sanitize_report(
        report, blackboard, run_id, question=question, report_problems=problems
    )
    report = report.model_copy(
        update={"confidence": min(report.confidence, assessment.confidence_ceiling)}
    )

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
    if assessment.limitations:
        ack["evidence_limitations"] = list(assessment.limitations)
    if partial:
        ack["partial"] = True
    ack["subject_specific"] = assessment.subject_specific
    ack["answer_as_asked"] = assessment.answer_as_asked
    ack["confidence_ceiling"] = assessment.confidence_ceiling
    if problems:
        ack["claim_flags"] = problems
    if fallback:
        ack["fallback"] = True
        ack["note"] = (
            "writer failed with "
            f"{type(failure).__name__}: {' '.join(str(failure).split())[:160]}"
        )
    if truncated:
        ack["truncated"] = True
    return ack

def _preferences_block() -> str:
    """The user's standing preferences, after the system rules."""
    from athena.settings.preferences import preference_block

    block = preference_block()
    return f"\n{block}\n\n" if block else ""