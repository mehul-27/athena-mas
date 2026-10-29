"""Deterministic evidence-sufficiency assessment for fixed research requirements.

This is deliberately conservative: snippets, RAG chunks from an unrelated
corpus, failed fetches, duplicate URLs, and secondary pages do not count as
independent primary-source support. The writer is blocked from claiming a
complete answer when this gate fails; the critic can still request recovery.
"""

from __future__ import annotations

from dataclasses import dataclass

from athena.agents.requirements import required_findings
from athena.agents.routing import classify, is_primary_source, is_relevant

MIN_CONTENT_CHARS = 120


@dataclass(frozen=True)
class Assessment:
    ok: bool
    route: str
    required: int
    supported: int
    distinct_sources: int
    primary_sources: int
    issues: tuple[str, ...]


def assess(question: str, items) -> Assessment:
    route = classify(question)
    required = required_findings(question)
    citable = [
        item for item in (items or [])
        if getattr(item, "source_type", "") in {"rag", "web", "calculation", "forecast"}
    ]

    fetched = [
        item for item in citable
        if getattr(item, "source_type", "") == "web"
        and str(getattr(item, "id", "")).startswith("fetch-")
        and str(getattr(item, "source_ref", "")).startswith(("http://", "https://"))
        and len((getattr(item, "content", "") or "").strip()) >= MIN_CONTENT_CHARS
        and is_relevant(getattr(item, "content", ""), question)
    ]
    rag = [
        item for item in citable
        if getattr(item, "source_type", "") == "rag"
        and len((getattr(item, "content", "") or "").strip()) >= MIN_CONTENT_CHARS
        and is_relevant(getattr(item, "content", ""), question)
    ]

    primary = [item for item in fetched if is_primary_source(item.source_ref)]
    if route.requires_official:
        pool = primary
    elif route.kind == "local":
        pool = rag
    else:
        pool = fetched or rag

    distinct = {}
    for item in pool:
        distinct.setdefault(item.source_ref, item)
    supported = len(distinct)

    issues: list[str] = []
    if route.kind == "external":
        if not fetched:
            issues.append(
                "No relevant fetched web pages were recorded; search snippets and "
                "irrelevant RAG chunks are not sufficient evidence."
            )
        if route.requires_official and not primary:
            issues.append(
                "The question requires official/primary sources, but no relevant "
                "successfully fetched primary-source page was recorded."
            )
        if required and supported < required:
            issues.append(
                f"The question requires {required} findings with distinct primary "
                f"sources; only {supported} qualifying source(s) were recorded."
            )
        if not required and supported < 1:
            issues.append("No relevant fetched primary-source evidence was recorded.")
    elif route.kind == "local":
        if not rag:
            issues.append(
                "No relevant local-document evidence was recorded for this question."
            )
        if required and supported < required:
            issues.append(
                f"The question requires {required} supported findings; only {supported} "
                "relevant local source section(s) were recorded."
            )
    else:
        if required and supported < required:
            issues.append(
                f"The question requires {required} supported findings; only "
                f"{supported} relevant source(s) were recorded."
            )

    return Assessment(
        ok=not issues,
        route=route.kind,
        required=required,
        supported=supported,
        distinct_sources=len({item.source_ref for item in fetched or rag}),
        primary_sources=len({item.source_ref for item in primary}),
        issues=tuple(issues),
    )


def gap_answer(question: str, assessment: Assessment) -> str:
    requirement = (
        f"{assessment.required} requested finding(s)" if assessment.required
        else "the requested evidence"
    )
    detail = "; ".join(assessment.issues) or "the available evidence is incomplete."
    return (
        f"The available evidence does not satisfy {requirement}. {detail} "
        "I have not inferred findings from titles, snippets, or unrelated documents, "
        "and I have not invented sources or URLs. A further research pass is needed "
        "to resolve the unsupported parts of the question."
    )
