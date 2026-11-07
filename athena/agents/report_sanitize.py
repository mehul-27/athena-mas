"""Make a generated FinalReport honest about its sources.

The model writes the prose, but it does not get to decide what counts as a
source or which tools ran: both are recomputed from the evidence actually on the
blackboard. A citation whose source_ref is not a recorded evidence item is
dropped.

What sanitizing *did* is recorded in ``FinalReport.guardrail_flags`` — removed
claims, removed citations and the ``screen_output`` flags for the text that
actually ships — so the report can account for itself instead of claiming a
guardrail ran (or claiming none did).
"""

from __future__ import annotations

from urllib.parse import urlparse

from athena.agents import bb_io
from athena.agents.claim_support import (
    advisory_problems,
    best_excerpt,
    best_support,
    remove_unsupported,
    split_sentences,
    validate_claims,
    claim_problems,
)
from athena.agents.routing import relevance
from athena.agents.sufficiency import named_terms
from athena.blackboard import Blackboard, Citation, FinalReport, GuardrailFlag
from athena.guardrails.output import screen_output

TOOL_FOR_SOURCE = {
    "rag": "rag_search",
    "web": "web_search",
    "calculation": "calculate",
    "forecast": "forecast",
}
CITABLE_SOURCE_TYPES = frozenset(TOOL_FOR_SOURCE)
STAGE = "final_output"


def _flag(rule: str, action: str, detail: str) -> GuardrailFlag:
    return GuardrailFlag(stage=STAGE, rule=rule, action=action, detail=detail[:300])


def _tool_for(item) -> str | None:
    """Tool attribution, including the fetch-specific web provenance id."""
    if item.source_type == "web" and item.id.startswith("fetch-"):
        return "fetch_page"
    return TOOL_FOR_SOURCE.get(item.source_type)


def _summary(item) -> str:
    text = " ".join(item.content.split())
    return text[:180] + ("..." if len(text) > 180 else "")


def _snippet_is_unread_pdf(item) -> bool:
    """A PDF search snippet is not a page read and cannot be cited as one."""
    if item.source_type != "web" or item.id.startswith("fetch-"):
        return False
    try:
        path = (urlparse(item.source_ref).path or "").lower()
    except ValueError:
        return False
    return path.endswith(".pdf")


def evidence_ids_for(report: FinalReport, items) -> list[str]:
    """Ids of the citable evidence items the report actually cites."""
    cited = {citation.source_ref for citation in report.citations}
    return [
        item.id
        for item in items
        if item.source_type in CITABLE_SOURCE_TYPES and item.source_ref in cited
    ]


def sanitize_report(
    report: FinalReport,
    blackboard: Blackboard,
    run_id: str,
    items=None,
    question: str | None = None,
    report_problems: list[str] | None = None,
) -> FinalReport:
    """Keep only citations backed by evidence, and derive ``tools_used``.

    ``items`` lets the p2p supervisor pass the evidence it is carrying, so the
    sanitizer never has to read the blackboard for it. ``report_problems`` is an
    optional out-parameter: advisory claim-level findings that survive
    sanitization (a superlative or a planned capability stated as existing) are
    appended there so the critic and the confidence calculation can see them.
    """
    items = bb_io.load_evidence(blackboard, limit=None) if items is None else list(items)
    citable = [
        item for item in items
        if item.source_type in CITABLE_SOURCE_TYPES
        and not _snippet_is_unread_pdf(item)
    ]
    citation_citable = [
        item for item in citable
        if not relevance(item.content, question or "").name_collision
    ]
    by_ref = {item.source_ref: item for item in citable}

    citations: list[Citation] = []
    uncited_refs: list[str] = []
    for citation in report.citations:
        evidence = by_ref.get(citation.source_ref)
        if evidence is None or relevance(evidence.content, question or "").name_collision:
            uncited_refs.append(citation.source_ref)
            continue
        citations.append(
            citation.model_copy(update={"source_type": evidence.source_type})
        )

    if not citations and citation_citable:
        citations = [
            Citation(
                source_type=evidence.source_type,
                source_ref=ref,
                quote_or_summary=_summary(evidence),
            )
            for ref, evidence in list(by_ref.items())
            if evidence in citation_citable
        ][:3]

    # Organization-specific claims can be supported by evidence the model
    # omitted from its small citation list. Backfill named-entity evidence,
    # preferring successfully fetched pages over search snippets, without
    # exceeding a readable citation set.
    named = named_terms(question or "")
    if named:
        cited_refs = {citation.source_ref for citation in citations}
        candidates = sorted(
            citation_citable,
            key=lambda item: 0
            if item.source_type == "web" and item.id.startswith("fetch-")
            else 1,
        )
        for item in candidates:
            if len(citations) >= 6:
                break
            if item.source_ref in cited_refs:
                continue
            blob = (item.content + " " + item.source_ref).lower()
            if not any(term.lower() in blob for term in named):
                continue
            # A historical/unrelated page with one matching name is not
            # organization-specific evidence and must not be cited as support.
            if relevance(item.content, question or "").name_collision:
                continue
            citations.append(
                Citation(
                    source_type=item.source_type,
                    source_ref=item.source_ref,
                    quote_or_summary=_summary(item),
                )
            )
            cited_refs.add(item.source_ref)

    cited_refs = {citation.source_ref for citation in citations}
    # Validate against every recorded citable source, not only the model's
    # initial citation list. A source omitted by the model can still support a
    # claim; the backfill below then adds its citation instead of deleting the
    # claim merely because of a citation-count limit.
    evidence_texts = [item.content for item in citable]

    answer, removed_answer = remove_unsupported(report.answer, evidence_texts)
    key_points: list[str] = []
    removed_points: list[str] = []
    for point in report.key_points:
        kept, removed = remove_unsupported(point, evidence_texts)
        if kept:
            key_points.append(kept)
        removed_points.extend(removed)

    answer_empty = not answer.strip()
    if removed_answer or removed_points:
        key_points.append(
            "Some generated claims were omitted because the captured evidence "
            "did not support them."
        )
    if answer_empty:
        answer = (
            "The captured evidence did not support the generated factual claims. "
            "The report has been reduced to its verified limitations."
        )
    if "[truncated]" in report.answer and not answer.rstrip().endswith("[truncated]"):
        answer = answer.rstrip() + " [truncated]"

    # Keep only citations that support at least one surviving factual sentence.
    factual_sentences = [
        sentence for sentence in split_sentences(answer)
        if not sentence.lower().startswith(("some generated", "the captured evidence"))
    ]
    kept_citations: list[Citation] = []
    unsupporting_refs: list[str] = []
    for citation in citations:
        evidence = by_ref.get(citation.source_ref)
        if evidence is None:
            unsupporting_refs.append(citation.source_ref)
            continue
        if factual_sentences and not any(
            best_support(sentence, [evidence.content]) >= 0.3
            for sentence in factual_sentences
        ):
            unsupporting_refs.append(citation.source_ref)
            continue
        kept_citations.append(
            citation.model_copy(
                update={
                    "source_type": evidence.source_type,
                    "quote_or_summary": (
                        best_excerpt(evidence.content, answer + " " + " ".join(key_points))
                        or _summary(evidence)
                    ),
                }
            )
        )
    citations = kept_citations
    # If a surviving factual sentence is supported only by a recorded source
    # that the model omitted from its citation list, add that source rather than
    # silently dropping the claim or leaving it uncited.
    cited_refs = {citation.source_ref for citation in citations}
    for sentence in factual_sentences:
        if any(
            best_support(sentence, [by_ref[ref].content]) >= 0.3
            for ref in cited_refs if ref in by_ref
        ):
            continue
        best_item = None
        best_score = 0.0
        for item in citation_citable:
            score = best_support(sentence, [item.content])
            if score > best_score:
                best_item, best_score = item, score
        if best_item is not None and best_score >= 0.3:
            citations.append(
                Citation(
                    source_type=best_item.source_type,
                    source_ref=best_item.source_ref,
                    quote_or_summary=(
                        best_excerpt(best_item.content, sentence) or _summary(best_item)
                    ),
                )
            )
            cited_refs.add(best_item.source_ref)

    confidence = report.confidence
    if removed_answer or removed_points:
        confidence = min(confidence, 0.55)
    if answer_empty:
        confidence = min(confidence, 0.3)

    # Claim-level validation of what actually ships: support, citation match,
    # entity relevance, and over-claims (superlatives, planned-capability
    # escalation). Over-claims are reported rather than silently deleted.
    checks = validate_claims(
        [answer, *key_points], citable, question or "", citations
    )
    if report_problems is not None:
        for check in checks:
            for problem in advisory_problems(
                check.claim, [item.content for item in citable]
            ):
                report_problems.append(f"{check.claim.strip()[:160]}: {problem}")
        report_problems.extend(
            problem for problem in claim_problems(checks) if problem not in report_problems
        )

    tools_used = sorted(
        {tool for item in items if (tool := _tool_for(item)) is not None}
    )
    flags = guardrail_flags_for(
        answer=answer,
        key_points=key_points,
        removed_claims=[*removed_answer, *removed_points],
        unbacked_refs=uncited_refs,
        unsupporting_refs=unsupporting_refs,
        truncated="[truncated]" in answer,
    )
    return report.model_copy(
        update={
            "answer": answer,
            "key_points": key_points,
            "citations": citations,
            "confidence": confidence,
            "tools_used": tools_used,
            "run_id": run_id,
            # Always derived here, never taken from the model: whatever the
            # writer put in this field is overwritten with what was actually
            # measured. An empty list therefore means "nothing fired", which is
            # only true when nothing did.
            "guardrail_flags": flags,
        }
    )


MAX_REFS_IN_DETAIL = 3


def _refs(refs: list[str]) -> str:
    unique = list(dict.fromkeys(refs))
    shown = ", ".join(unique[:MAX_REFS_IN_DETAIL])[:200]
    extra = len(unique) - MAX_REFS_IN_DETAIL
    return shown + (f" (+{extra} more)" if extra > 0 else "")


def guardrail_flags_for(
    *,
    answer: str,
    key_points: list[str] | None = None,
    removed_claims: list[str] | None = None,
    unbacked_refs: list[str] | None = None,
    unsupporting_refs: list[str] | None = None,
    truncated: bool = False,
) -> list[GuardrailFlag]:
    """The flags a sanitized report must carry.

    Deterministic and independent of the model: removal counts come from the
    sanitizer's own work, and the output screening runs over the text that
    actually ships, not the text the model wanted to ship.
    """
    flags: list[GuardrailFlag] = []
    claims = list(removed_claims or [])
    if claims:
        flags.append(
            _flag(
                "report.unsupported_claim",
                "sanitized",
                f"removed {len(claims)} unsupported claim(s): {claims[0][:160]}",
            )
        )
    unbacked = list(unbacked_refs or [])
    if unbacked:
        flags.append(
            _flag(
                "report.unrecorded_citation",
                "sanitized",
                f"dropped {len(unbacked)} citation(s) with no recorded evidence: "
                + _refs(unbacked),
            )
        )
    unsupporting = list(unsupporting_refs or [])
    if unsupporting:
        flags.append(
            _flag(
                "report.unsupported_citation",
                "sanitized",
                f"dropped {len(unsupporting)} citation(s) that supported no "
                "surviving claim: " + _refs(unsupporting),
            )
        )
    if truncated:
        flags.append(
            _flag("report.truncated", "warned", "the generated answer was truncated")
        )
    screened = screen_output(
        {"answer": answer, "key_points": list(key_points or [])},
        answer=answer,
        # This is the report sanitiser, not the chat answer: label the row
        # ``report_sanitize`` so the audit trail names the stage that screened it.
        pipeline_stage="report_sanitize",
    )
    flags.extend(screened)
    seen: set[tuple[str, str]] = set()
    ordered: list[GuardrailFlag] = []
    for flag in flags:
        key = (flag.rule, flag.detail)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(flag)
    return ordered

