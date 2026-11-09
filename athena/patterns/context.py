"""The p2p message: everything the next stage needs, carried inline.

Order matters more than size. A stage's context is assembled by priority and
trimmed from the bottom, so the material the stage cannot work without - above
all the previous stage's output - always survives. Before this, evidence was
added first and the cap cut the writer's report off the end of the critic's
message.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from athena.agents.bb_io import select_critic_evidence
from athena.agents.untrusted import (
    UNTRUSTED_BEGIN,
    UNTRUSTED_END,
    wrap_untrusted,
)

QUESTION_BEGIN = "QUESTION:\n"
QUESTION_END = "===== END TASK QUESTION ====="
MAX_EVIDENCE_ITEMS = 8
MAX_EVIDENCE_CHARS = 600
MAX_FETCHED_EVIDENCE_CHARS = 2000
MAX_PLAN_STEPS = 8
DEFAULT_CONTEXT_CHARS = 9000
P2P_CONTEXT_LIMIT = 9000
CITABLE_EVIDENCE_TYPES = frozenset({"rag", "web", "calculation", "forecast"})
_UNTRUSTED_WRAPPER_CHARS = len(UNTRUSTED_BEGIN) + len(UNTRUSTED_END) + 2


def context_budget() -> int:
    value = (os.getenv("ATHENA_P2P_CONTEXT_CHARS") or "").strip()
    budget = int(value) if value.isdigit() else DEFAULT_CONTEXT_CHARS
    return min(budget, P2P_CONTEXT_LIMIT)


def question_block(question: str) -> str:
    """Render the task question inside an explicit, extractable boundary.

    P2P carries everything inline in one string, so the finding-count rule needs
    a boundary that excludes evidence, analysis and the report. The question is
    user text; a spoofed end marker is removed rather than trusted.
    """
    safe = (question or "").replace(QUESTION_END, "[boundary marker removed]")
    return f"{QUESTION_BEGIN}{safe}\n{QUESTION_END}"


def question_from_context(context: str) -> str | None:
    """The task question from a P2P context, or None when no boundary is present."""
    if not context or not context.startswith(QUESTION_BEGIN):
        return None
    start = len(QUESTION_BEGIN)
    end = context.find("\n" + QUESTION_END, start)
    if end < 0:
        return None
    return context[start:end]


def task_question(instruction: str, context: str) -> str:
    """The question-only text used for finding-count requirements.

    Production P2P calls always carry the question boundary, so evidence,
    analysis and the report are never scanned. A direct role call without that
    boundary keeps the legacy behaviour: the instruction is the only task text
    available. It is never the evidence or another stage's output.
    """
    found = question_from_context(context)
    if found is None:
        return instruction or ""
    return found


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
    tool_status: str = ""
    limitations: list[str] = field(default_factory=list)
    evidence_limitations: list[str] = field(default_factory=list)
    blocking_issues: list[str] = field(default_factory=list)
    partial_answer: bool = False
    coverage: str = ""
    critique_verdict: Any = None
    critique_feedback: str = ""

    def report_citations(self) -> list:
        """Citations from the inline report, or an empty list when unavailable."""
        if not self.report:
            return []
        try:
            from athena.blackboard import FinalReport

            return list(FinalReport.model_validate(json.loads(self.report)).citations)
        except Exception:  # noqa: BLE001 - a malformed report has no audit
            return []

    def evidence_block(self, limit: int | None = None) -> str:
        """Evidence plus an honest citation audit, bounded to ``limit`` chars.

        Cited sources are selected first within the unchanged eight-item cap.
        The audit is recomputed after character trimming, so it never calls an
        excerpt present when the budget dropped it.
        """
        citations = self.report_citations()
        if not self.evidence and not citations:
            return "(no evidence recorded)"
        citable = [
            item
            for item in self.evidence
            if item.source_type in CITABLE_EVIDENCE_TYPES
        ]
        if not citable and not citations:
            return "(no citable evidence recorded)"

        budget = None if limit is None else max(0, limit - _UNTRUSTED_WRAPPER_CHARS)
        for _ in range(MAX_EVIDENCE_ITEMS + 2):
            lines, audit = select_critic_evidence(
                self.evidence,
                citations,
                max_chars=MAX_EVIDENCE_CHARS,
                fetched_max_chars=MAX_FETCHED_EVIDENCE_CHARS,
                max_total_chars=budget,
            )
            evidence = (
                wrap_untrusted("\n".join(lines))
                if lines
                else "(no citable evidence recorded)"
            )
            audit_text = "\n".join(audit) if citations else ""
            block = "\n".join(
                part
                for part in (
                    audit_text,
                    "Evidence (report citations prioritized):" if citations else "",
                    evidence,
                )
                if part
            )
            if limit is None or len(block) <= limit:
                return block
            overflow = len(block) - limit
            new_budget = max(0, (budget or 0) - overflow - 1)
            if new_budget == budget:
                break
            budget = new_budget
        return "(evidence omitted: context full)"

    def status_block(self) -> str:
        """Actual tool status and unmet requirements, never inferred usage."""
        parts: list[str] = []
        if self.tool_status:
            parts.append(f"TOOL AND EVIDENCE STATUS:\n{self.tool_status}")
        if self.limitations:
            parts.append(
                "UNMET REQUIREMENTS:\n"
                + "\n".join(f"- {item}" for item in self.limitations)
            )
        if self.evidence_limitations:
            parts.append(
                "NON-BLOCKING EVIDENCE LIMITATIONS:\n"
                + "\n".join(f"- {item}" for item in self.evidence_limitations)
            )
        if self.blocking_issues:
            parts.append(
                "UNRESOLVED EVIDENCE REQUIREMENTS (do not claim them):\n"
                + "\n".join(f"- {item}" for item in self.blocking_issues)
            )
        if self.coverage:
            parts.append(f"EVIDENCE COVERAGE:\n{self.coverage}")
        if self.partial_answer:
            parts.append(
                "PARTIAL ANSWER MODE: only search-result snippets support the "
                "general principle. Do not claim any page was read. Answer the "
                "supported general principle, state that SpaceX-specific details "
                "remain unverified, and do not return a total refusal."
            )
        return "\n\n".join(parts)

    def for_role(self, role: str) -> str:
        """The context block a given role receives, highest priority first."""
        head = question_block(self.question)
        if role == "analyst":
            blocks = [head]
            if self.plan:
                blocks.append(f"PLAN:\n{self.plan}")
            if self.status_block():
                blocks.append(self.status_block())
            blocks.append("EVIDENCE:\n" + self.evidence_block())
            return "\n\n".join(blocks)

        # The writer needs the analysis and, on a revision, the calibrated
        # critique. The critic needs the report *and* the analysis. These are
        # never trimmed.
        stage = []
        if role == "critic" and self.report:
            stage.append(f"REPORT:\n{self.report}")
        if role == "writer" and self.critique_feedback:
            stage.append(
                "PREVIOUS CRITIQUE (fix every listed issue):\n"
                + self.critique_feedback
            )
        if self.analysis:
            stage.append(f"ANALYSIS:\n{self.analysis}")
        stage_block = "\n\n".join(stage)

        plan_block = f"PLAN:\n{self.plan}" if self.plan else ""
        status_block = self.status_block()
        fixed = "\n\n".join(
            part for part in (head, stage_block, plan_block, status_block) if part
        )
        room = max(0, context_budget() - len(fixed) - len("\n\nEVIDENCE:\n"))
        body = "\n\n".join(
            part for part in (head, stage_block, plan_block, status_block,
                              "EVIDENCE:\n" + self.evidence_block(room))
            if part
        )
        return body[: context_budget()]
