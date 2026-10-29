"""The p2p message: everything the next stage needs, carried inline.

Order matters more than size. A stage's context is assembled by priority and
trimmed from the bottom, so the material the stage cannot work without - above
all the previous stage's output - always survives. Before this, evidence was
added first and the cap cut the writer's report off the end of the critic's
message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from athena.agents.untrusted import wrap_untrusted

MAX_EVIDENCE_ITEMS = 8
MAX_EVIDENCE_CHARS = 600
MAX_PLAN_STEPS = 8
DEFAULT_CONTEXT_CHARS = 9000
CITABLE_EVIDENCE_TYPES = frozenset({"rag", "web", "calculation", "forecast"})


def context_budget() -> int:
    value = (os.getenv("ATHENA_P2P_CONTEXT_CHARS") or "").strip()
    return int(value) if value.isdigit() else DEFAULT_CONTEXT_CHARS


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

    def evidence_block(self, limit: int | None = None) -> str:
        if not self.evidence:
            return "(no evidence recorded)"
        items = [item for item in self.evidence if item.source_type in CITABLE_EVIDENCE_TYPES]
        items.sort(key=lambda item: (
            0 if item.source_type == "web" and item.id.startswith("fetch-")
            else 1 if item.source_type == "rag" else 2
        ))
        if not items:
            return "(no citable evidence recorded)"
        lines = []
        used = 0
        for item in items[:MAX_EVIDENCE_ITEMS]:
            text = " ".join(item.content.split())
            if len(text) > MAX_EVIDENCE_CHARS:
                text = text[:MAX_EVIDENCE_CHARS].rstrip() + "..."
            line = f"[{item.id}] {item.source_type} | {item.source_ref} | {text}"
            if limit is not None and used + len(line) > limit:
                break
            lines.append(line)
            used += len(line) + 1
        body = wrap_untrusted("\n".join(lines)) if lines else "(evidence omitted: context full)"
        if limit is not None and len(body) > limit:
            return "(evidence omitted: context full)"
        return body

    def for_role(self, role: str) -> str:
        """The context block a given role receives, highest priority first."""
        if role == "analyst":
            blocks = [f"QUESTION:\n{self.question}"]
            if self.plan:
                blocks.append(f"PLAN:\n{self.plan}")
            blocks.append("EVIDENCE:\n" + self.evidence_block())
            return "\n\n".join(blocks)

        # The writer needs the analysis; the critic needs the report *and* the
        # analysis. These are never trimmed.
        stage = []
        if role == "critic" and self.report:
            stage.append(f"REPORT:\n{self.report}")
        if self.analysis:
            stage.append(f"ANALYSIS:\n{self.analysis}")
        stage_block = "\n\n".join(stage)

        head = f"QUESTION:\n{self.question}"
        plan_block = f"PLAN:\n{self.plan}" if self.plan else ""
        fixed = "\n\n".join(part for part in (head, stage_block, plan_block) if part)
        room = max(0, context_budget() - len(fixed) - len("\n\nEVIDENCE:\n"))
        body = "\n\n".join(
            part for part in (head, stage_block, plan_block,
                              "EVIDENCE:\n" + self.evidence_block(room))
            if part
        )
        return body[: context_budget()]
