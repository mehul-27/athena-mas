"""Make a generated FinalReport honest about its sources.

The model writes the prose, but it does not get to decide what counts as a
source or which tools ran: both are recomputed from the evidence actually on the
blackboard. A citation whose source_ref is not a recorded evidence item is
dropped.
"""

from __future__ import annotations

from athena.agents import bb_io
from athena.blackboard import Blackboard, Citation, FinalReport

TOOL_FOR_SOURCE = {
    "rag": "rag_search",
    "web": "web_search",
    "calculation": "calculate",
    "forecast": "forecast",
}


def _summary(item) -> str:
    text = " ".join(item.content.split())
    return text[:180] + ("..." if len(text) > 180 else "")


def sanitize_report(report: FinalReport, blackboard: Blackboard, run_id: str) -> FinalReport:
    """Keep only citations backed by evidence, and derive ``tools_used``."""
    items = bb_io.load_evidence(blackboard, limit=None)
    by_ref = {item.source_ref: item for item in items}

    citations: list[Citation] = []
    for citation in report.citations:
        evidence = by_ref.get(citation.source_ref)
        if evidence is None:
            continue
        citations.append(
            citation.model_copy(update={"source_type": evidence.source_type})
        )

    if not citations and by_ref:
        citations = [
            Citation(
                source_type=evidence.source_type,
                source_ref=ref,
                quote_or_summary=_summary(evidence),
            )
            for ref, evidence in list(by_ref.items())[:3]
        ]

    tools_used = sorted(
        {TOOL_FOR_SOURCE[item.source_type] for item in items if item.source_type in TOOL_FOR_SOURCE}
    )
    return report.model_copy(
        update={
            "citations": citations,
            "tools_used": tools_used,
            "run_id": run_id,
            # No guardrail layer exists yet (Step 8), so nothing may claim one
            # ran. The model fills this field with invented flags otherwise.
            "guardrail_flags": [],
        }
    )
