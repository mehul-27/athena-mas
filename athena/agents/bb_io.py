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
MAX_FETCHED_EVIDENCE_CHARS = 2000
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
        # Fetched pages carry navigation before the useful body. Keep enough
        # text to reach the official highlights; snippets stay compact.
        cap = MAX_FETCHED_EVIDENCE_CHARS if item.id.startswith("fetch-") else max_chars
        if len(text) > cap:
            text = text[:cap].rstrip() + "..."
        lines.append(f"[{item.id}] {item.source_type} | {item.source_ref} | {text}")
    return wrap_untrusted("\n".join(lines))


def _rendered_evidence_line(
    item,
    max_chars: int = MAX_EVIDENCE_CHARS,
    fetched_max_chars: int = MAX_FETCHED_EVIDENCE_CHARS,
) -> str:
    """One compact prompt line for an evidence item, with its own text cap."""
    text = " ".join(item.content.split())
    cap = fetched_max_chars if item.id.startswith("fetch-") else max_chars
    if len(text) > cap:
        text = text[:cap].rstrip() + "..."
    return f"[{item.id}] {item.source_type} | {item.source_ref} | {text}"


def select_critic_evidence(
    evidence,
    citations=(),
    *,
    limit: int = MAX_EVIDENCE_ITEMS,
    max_chars: int = MAX_EVIDENCE_CHARS,
    fetched_max_chars: int = MAX_FETCHED_EVIDENCE_CHARS,
    max_total_chars: int | None = None,
) -> tuple[list[str], list[str]]:
    """Select citation-prioritized evidence and audit what is actually excerpted.

    The item count and per-item text limits match ``evidence_text``. Report
    citations reserve one best item each before the remaining slots are filled.
    ``max_total_chars`` bounds the rendered line body; the audit reflects the
    excerpts that survive that bound, not merely the pre-trim selection.

    Returns ``(lines, audit)``. The caller owns the untrusted-content boundary:
    every line contains a report-controlled source reference.
    """
    all_items = list(evidence or [])
    citations = list(citations or ())
    recorded_refs = {item.source_ref for item in all_items}
    citable_items = [
        item for item in all_items if item.source_type in CITABLE_EVIDENCE_TYPES
    ]
    citable_refs = {item.source_ref for item in citable_items}
    cited_order = {citation.source_ref: index for index, citation in enumerate(citations)}

    # Reserve one best item per cited source before filling the remaining
    # slots, so many chunks from an early source cannot crowd out later refs.
    best_by_ref = {}
    for item in sorted(citable_items, key=_evidence_priority):
        best_by_ref.setdefault(item.source_ref, item)
    selected = [
        best_by_ref[ref]
        for ref in cited_order
        if ref in best_by_ref
    ][:limit]
    selected_ids = {item.id for item in selected}
    remaining = sorted(
        (item for item in citable_items if item.id not in selected_ids),
        key=_evidence_priority,
    )
    selected.extend(remaining[: limit - len(selected)])

    lines: list[str] = []
    used = 0
    excerpted_refs: set[str] = set()
    for item in selected:
        line = _rendered_evidence_line(item, max_chars, fetched_max_chars)
        addition = len(line) + (1 if lines else 0)
        if max_total_chars is not None and used + addition > max_total_chars:
            break
        lines.append(line)
        used += addition
        excerpted_refs.add(item.source_ref)

    audit = ["Citation reference audit (presence only; not support verification):"]
    for index, citation in enumerate(citations, start=1):
        if citation.source_ref not in recorded_refs:
            status = "missing from recorded evidence"
        elif citation.source_ref not in citable_refs:
            status = "recorded, but not citable evidence"
        elif citation.source_ref not in excerpted_refs:
            status = "recorded but outside the evidence excerpt budget"
        else:
            status = "recorded and excerpted"
        audit.append(f"- Citation {index}: {status}")
    return lines, audit


def critic_evidence_text(blackboard: Blackboard) -> str:
    """Give the critic citation-aware evidence within the usual context budget.

    Report references are ranked first, but the evidence item count and text
    limits stay the same as ``evidence_text``. The audit exposes only whether
    each report citation resolves to recorded evidence; presence is not proof
    that its content supports the claim.
    """
    report = section(blackboard, "final_report")
    citations = report.citations if report is not None else []
    lines, audit = select_critic_evidence(
        load_evidence(blackboard, limit=None), citations
    )
    evidence = (
        wrap_untrusted("\n".join(lines))
        if lines
        else "(no citable evidence recorded)"
    )
    return "\n".join((*audit, "Evidence (report citations prioritized):", evidence))


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
    return report.model_dump_json(indent=2)


def critique_text(blackboard: Blackboard) -> str:
    critique = section(blackboard, "critique")
    if critique is None:
        return ""
    issues = "\n".join(f"- {item}" for item in critique.issues) or "- (none listed)"
    return f"approved={str(critique.approved).lower()} severity={critique.severity}\n{issues}"


def evidence_by_ref(blackboard: Blackboard) -> dict[str, EvidenceItem]:
    """Every recorded evidence item, keyed by its source_ref (for citation checks)."""
    return {item.source_ref: item for item in load_evidence(blackboard, limit=None)}
