"""The writer role: turn task + evidence into a Draft and a FinalReport.

The model writes the prose; ``sanitize_report`` decides what counts as a source
and which tools ran. Invalid structured output gets one correction attempt and
a minimal report fallback. Provider and local-limit failures propagate.
"""

from __future__ import annotations

from typing import Callable

from pydantic import ValidationError

from athena.agents import bb_io
from athena.agents.crew_roles import crew_call, unpack_result
from athena.agents.report_sanitize import redact_final_report, sanitize_report
from athena.agents.requirements import finding_requirement, required_findings
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


class _InvalidReportOutput(Exception):
    def __init__(self, summary: str, stats: dict | None = None) -> None:
        super().__init__(summary)
        self.stats = stats or {}


def _validation_error(exc: BaseException) -> ValidationError | None:
    """Find only a validation failure for this exact report schema."""
    if isinstance(exc, ValidationError) and exc.title == "FinalReport":
        return exc
    # CrewAI's converter wraps failed output validation in this concrete type.
    # Do not walk arbitrary exception chains: provider failures may be chained
    # from an earlier validation problem and must still propagate.
    try:
        from crewai.utilities.converter import ConverterError
    except ImportError:  # pragma: no cover - CrewAI is optional for offline users
        return None
    cause = exc.__cause__ or exc.__context__
    if isinstance(exc, ConverterError) and isinstance(cause, ValidationError):
        if cause.title == "FinalReport":
            return cause
    return None


def _validation_summary(exc: ValidationError) -> str:
    """Keep correction prompts and retry events free of model-returned values."""
    from athena.guardrails.secrets import redact_text

    details = []
    for error in exc.errors(include_input=False):
        location = ".".join(str(part) for part in error.get("loc", ())) or "report"
        details.append(f"{location}: {error.get('msg', 'invalid value')}")
    summary = "FinalReport validation failed: " + "; ".join(details[:8])
    return redact_text(summary) or "FinalReport validation failed"


def _parse_generated_report(value: object) -> tuple[FinalReport, dict]:
    report, stats = unpack_result(value)
    if isinstance(report, FinalReport):
        return report, stats
    try:
        parsed = (
            FinalReport.model_validate_json(report)
            if isinstance(report, str)
            else FinalReport.model_validate(report)
        )
    except ValidationError as exc:
        if exc.title != "FinalReport":
            raise
        raise _InvalidReportOutput(_validation_summary(exc), stats) from exc
    return parsed, stats


def _generate_validated(generate: Generate, prompt: str) -> tuple[FinalReport, dict]:
    try:
        value = generate(prompt)
    except Exception as exc:
        validation = _validation_error(exc)
        if validation is None:
            raise
        raise _InvalidReportOutput(_validation_summary(validation)) from exc
    return _parse_generated_report(value)


def _merge_stats(*values: dict) -> dict:
    merged: dict = {}
    for key in ("tokens_in", "tokens_out", "reasoning_tokens", "calls", "model_ms"):
        present = [value[key] for value in values if isinstance(value.get(key), (int, float))]
        if present:
            merged[key] = sum(present)
    if any(value.get("estimated") for value in values):
        merged["estimated"] = True
    return merged


def _fallback_report(run_id: str) -> FinalReport:
    """A legal FinalReport so the pipeline finishes instead of crashing."""
    return FinalReport(
        answer=(
            "The report could not be generated because the writer's structured "
            "output remained invalid after one correction attempt. The evidence "
            "remains on the blackboard for inspection."
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
    requested_findings = required_findings(bb_io.task_text(blackboard))
    output_shape = (
        f"exactly {requested_findings} key points/findings and at least "
        f"{requested_findings} citations, with a distinct primary-source URL "
        "for each finding"
        if requested_findings
        else "3-5 key points and 1-3 citations"
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
        f"full stop, {output_shape}; copy each citation's source_ref verbatim "
        "from the evidence lines above. confidence is 0 to 1. "
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
        report = redact_final_report(report)
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
    problems: list[str] = []
    try:
        report, stats = _generate_validated(generate, prompt)
    except _InvalidReportOutput as exc:
        try:
            # The second call below is a real retry of the same work, so it is
            # reported as one (and only one) ``retry`` event before it runs.
            retry_invalid_output(str(exc), agent="writer")
            report, retry_stats = _generate_validated(
                generate,
                f"{prompt}\n\nYour previous output was rejected: {exc}\n"
                "Return only valid JSON.",
            )
            stats = _merge_stats(exc.stats, retry_stats)
        except _InvalidReportOutput as exc2:
            stats = _merge_stats(exc.stats, exc2.stats)
            report = _fallback_report(run_id)
            fallback = True
    # A cut-off answer is a real defect: retry once, then mark it.
    truncated = False
    if looks_truncated(report.answer):
        # One re-generation is about to happen, so it is one ``retry`` event —
        # emitted here, in the branch that decided to retry, and only here.
        retry_truncated_report(agent="writer")
        try:
            retry, retry_stats = _generate_validated(generate, prompt + SHORTER)
            report = retry
            stats = _merge_stats(stats, retry_stats)
        except _InvalidReportOutput as exc:
            # The first report is still available and will be marked truncated.
            stats = _merge_stats(stats, exc.stats)
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
        ack["note"] = "writer output remained invalid after one correction attempt"
    if truncated:
        ack["truncated"] = True
    return ack

def _preferences_block() -> str:
    """The user's standing preferences, after the system rules."""
    from athena.settings.preferences import preference_block

    block = preference_block()
    return f"\n{block}\n\n" if block else ""
