"""The p2p message: everything the next stage needs, carried inline.

Rendering mirrors ``athena/agents/bb_io`` (same truncation limits) so the p2p
and blackboard results stay comparable — the difference is the transport, not
how much text the model sees.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_EVIDENCE_ITEMS = 8
MAX_EVIDENCE_CHARS = 600
MAX_PLAN_STEPS = 8


def render_plan(plan) -> str:
    if not plan or not plan.steps:
        return "(no plan)"
    return "\n".join(
        f"{index}. {step.description} (owner: {step.assigned_to})"
        for index, step in enumerate(plan.steps[:MAX_PLAN_STEPS], start=1)
    )


@dataclass
class P2PContext:
    """The message body: grown by the supervisor after each stage."""

    question: str
    plan: str = ""
    evidence: list = field(default_factory=list)
    analysis: str = ""
    report: str = ""
    critique: str = ""

    def evidence_block(self) -> str:
        if not self.evidence:
            return "(no evidence recorded)"
        lines = []
        for item in self.evidence[:MAX_EVIDENCE_ITEMS]:
            text = " ".join(item.content.split())
            if len(text) > MAX_EVIDENCE_CHARS:
                text = text[:MAX_EVIDENCE_CHARS].rstrip() + "..."
            lines.append(f"[{item.id}] {item.source_type} | {item.source_ref} | {text}")
        return "\n".join(lines)

    def for_role(self, role: str) -> str:
        """The context block a given role receives."""
        parts = [f"QUESTION:\n{self.question}"]
        if role in {"writer", "critic"} and self.plan:
            parts.append(f"PLAN:\n{self.plan}")
        parts.append(f"EVIDENCE:\n{self.evidence_block()}")
        if role in {"writer", "critic"} and self.analysis:
            parts.append(f"ANALYSIS:\n{self.analysis}")
        if role == "critic" and self.report:
            parts.append(f"REPORT:\n{self.report}")
        return "\n\n".join(parts)
