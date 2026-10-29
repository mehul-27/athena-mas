"""Blackboard helpers shared by the ADK agents and the CrewAI roles.

Everything an agent needs to build a prompt comes through here, so both
frameworks read the blackboard the same way. Rendering is deliberately lossy:
evidence is truncated and bounded, because the full text belongs in the
blackboard, not in a prompt.
"""

from __future__ import annotations

from athena.agents.untrusted import wrap_untrusted
from athena.blackboard import Analysis, Blackboard, EvidenceItem, Task

MAX_EVIDENCE_ITEMS = 8
MAX_EVIDENCE_CHARS = 600
MAX_PLAN_STEPS = 8
CITABLE_EVIDENCE_TYPES = frozenset({"rag", "web", "calculation", "forecast"})


_READS_FORBIDDEN = False


class BlackboardReadForbidden(RuntimeError):
    """Raised when an agent that must stay off the blackboard reads it."""


def forbid_reads(value: bool = True) -> None:
    """Test hook: make every read helper raise (p2p agents must not read)."""
    global _READS_FORBIDDEN
    _READS_FORBIDDEN = value


def section(blackboard: Blackboard, name: str):
    """The current value of a section, or None when it has never been written."""
    if _READS_FORBIDDEN:
        raise BlackboardReadForbidden(f"agent read blackboard section {name!r} in p2p mode")
    found = blackboard.read(name)
    return found[0] if found else None


def load_evidence(blackboard: Blackboard, limit: int | None = MAX_EVIDENCE_ITEMS) -> list[EvidenceItem]:
    items = section(blackboard, "evidence") or []
    return list(items if limit is None else items[:limit])


def task_text(blackboard: Blackboard) -> str:
    task: Task | None = section(blackboard, "task")
    if task is None:
        return "(no task recorded)"
    persona = f"\nPersona: {task.persona}" if task.persona else ""
    return f"Question: {task.question}\nPattern: {task.pattern}{persona}"


def plan_text(blackboard: Blackboard) -> str:
    plan = section(blackboard, "plan")
    if plan is None or not plan.steps:
        return "(no plan recorded)"
    return "\n".join(
        f"{index}. {step.description} (owner: {step.assigned_to})"
        for index, step in enumerate(plan.steps[:MAX_PLAN_STEPS], start=1)
    )


def _evidence_priority(item) -> int:
    """Fetched pages first, then RAG, then search snippets."""
    if item.source_type == "web" and item.id.startswith("fetch-"):
        return 0
    if item.source_type == "rag":
        return 1
    return 2


def evidence_text(
    blackboard: Blackboard,
    limit: int = MAX_EVIDENCE_ITEMS,
    max_chars: int = MAX_EVIDENCE_CHARS,
) -> str:
    """Evidence as compact prompt lines: id, kind, source ref, truncated text."""
    items = load_evidence(blackboard, limit=None)
    if not items:
        return "(no evidence recorded)"
    items = [item for item in items if item.source_type in CITABLE_EVIDENCE_TYPES]
    items = sorted(items, key=_evidence_priority)[:limit]
    if not items:
        return "(no citable evidence recorded)"

    lines = []
    for item in items:
        text = " ".join(item.content.split())
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "..."
        lines.append(f"[{item.id}] {item.source_type} | {item.source_ref} | {text}")
    return wrap_untrusted("\n".join(lines))


def analysis_text(blackboard: Blackboard) -> str:
    analysis: Analysis | None = section(blackboard, "analysis")
    if analysis is None:
        return "(no analysis yet)"
    findings = "\n".join(f"- {item}" for item in analysis.key_findings) or "- (none)"
    gaps = "\n".join(f"- {item}" for item in analysis.gaps) or "- (none)"
    return f"Key findings:\n{findings}\nGaps:\n{gaps}"


def draft_text(blackboard: Blackboard) -> str:
    draft = section(blackboard, "draft")
    return draft.text if draft else "(no draft yet)"


def report_text(blackboard: Blackboard) -> str:
    report = section(blackboard, "final_report")
    if report is None:
        return "(no final report yet)"
    citations = ", ".join(citation.source_ref for citation in report.citations) or "(none)"
    return f"Answer: {report.answer}\nConfidence: {report.confidence}\nCitations: {citations}"


def critique_text(blackboard: Blackboard) -> str:
    critique = section(blackboard, "critique")
    if critique is None:
        return ""
    issues = "\n".join(f"- {item}" for item in critique.issues) or "- (none listed)"
    return f"approved={str(critique.approved).lower()} severity={critique.severity}\n{issues}"


def evidence_by_ref(blackboard: Blackboard) -> dict[str, EvidenceItem]:
    """Every recorded evidence item, keyed by its source_ref (for citation checks)."""
    return {item.source_ref: item for item in load_evidence(blackboard, limit=None)}
