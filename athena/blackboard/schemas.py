"""Pydantic v2 models shared by every Athena agent.

These are the only shapes written to the blackboard, and ``FinalReport`` is the
structured final output the course requires. Agents run in separate processes,
so this module is the contract between them: a ``run_id`` and a section name
are enough to read or write any of it.

The module also owns the section registry: which section holds which model,
and which sections are append-only lists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

SourceType = Literal["rag", "web", "calculation", "forecast", "other"]
StepStatus = Literal["pending", "running", "done", "failed"]
Severity = Literal["low", "medium", "high"]
GuardrailStage = Literal["input", "tool_output", "final_output"]
GuardrailAction = Literal["blocked", "sanitized", "warned"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Model(BaseModel):
    """Base model. Unknown fields are rejected so typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


class Task(_Model):
    """The research question that starts a run."""

    run_id: str = Field(
        default="",
        description="Run identifier. Left empty on creation; the store fills it in.",
    )
    question: str = Field(description="The user's research question.")
    pattern: str = Field(
        default="sequential",
        description="Agentic pattern to use (free text for now: sequential, "
        "parallel, hierarchical).",
    )
    persona: str | None = Field(
        default=None, description="Optional persona the answer should adopt."
    )
    created_at: datetime = Field(
        default_factory=_utcnow, description="Creation time, UTC."
    )


class PlanStep(_Model):
    """One unit of work in the planner's plan."""

    id: str = Field(description="Stable step identifier, unique within the plan.")
    description: str = Field(description="What this step must produce.")
    assigned_to: str = Field(description="Name of the agent responsible for the step.")
    status: StepStatus = Field(
        default="pending", description="Lifecycle state of the step."
    )


class Plan(_Model):
    """The planner's decomposition of the task."""

    steps: list[PlanStep] = Field(
        default_factory=list, description="Ordered steps to execute."
    )


class EvidenceItem(_Model):
    """One piece of evidence gathered by a researcher."""

    id: str = Field(description="Stable evidence identifier, unique within the run.")
    source_type: SourceType = Field(description="Where this evidence came from.")
    source_ref: str = Field(
        description="Document id, URL or tool name the evidence came from."
    )
    content: str = Field(description="The retrieved text or computed value.")
    score: float | None = Field(
        default=None, description="Relevance or confidence score, if the source gave one."
    )
    retrieved_by: str = Field(description="Name of the agent that retrieved it.")


class Draft(_Model):
    """The writer's draft answer, before critique."""

    text: str = Field(description="Draft answer text.")
    based_on_evidence_ids: list[str] = Field(
        default_factory=list,
        description="Ids of the evidence items this draft relies on.",
    )


class Critique(_Model):
    """The critic's review of a draft."""

    issues: list[str] = Field(
        default_factory=list, description="Problems found, one sentence each."
    )
    severity: Severity = Field(description="How bad the worst issue is.")
    approved: bool = Field(description="Whether the draft may go to the user.")


class Analysis(_Model):
    """The analyst's synthesis of the evidence, before anything is written."""

    key_findings: list[str] = Field(
        default_factory=list, description="What the evidence establishes."
    )
    gaps: list[str] = Field(
        default_factory=list, description="What is missing or unresolved."
    )


class GuardrailFlag(_Model):
    """A guardrail decision recorded for audit."""

    stage: GuardrailStage = Field(description="Pipeline stage where it fired.")
    rule: str = Field(description="Identifier of the rule that fired.")
    action: GuardrailAction = Field(description="What the guardrail did.")
    detail: str = Field(description="Human-readable explanation.")


class Citation(_Model):
    """A citation backing the final answer."""

    source_type: SourceType = Field(description="Kind of source cited.")
    source_ref: str = Field(description="Document id, URL or tool name.")
    quote_or_summary: str = Field(
        description="Short quote from the source, or a summary of what it said."
    )


class FinalReport(_Model):
    """The structured final answer: the course's required output shape."""

    answer: str = Field(description="The answer, in prose, addressed to the user.")
    key_points: list[str] = Field(default_factory=list, description="The main takeaways.")
    citations: list[Citation] = Field(default_factory=list, description="Sources backing the answer.")
    confidence: float = Field(ge=0.0, le=1.0, description="Self-reported confidence, 0 to 1.")
    tools_used: list[str] = Field(default_factory=list, description="Tools invoked during the run.")
    guardrail_flags: list[GuardrailFlag] = Field(
        default_factory=list, description="Guardrail decisions taken during the run."
    )
    run_id: str = Field(description="The run this report belongs to.")

    def to_markdown(self) -> str:
        """Render the report as readable Markdown, including a citations table."""
        lines = ["# Athena Research Report", "", "## Answer", "", self.answer, ""]

        lines += ["## Key points", ""]
        lines += [f"- {point}" for point in self.key_points] or ["_None recorded._"]

        lines += ["", "## Citations", ""]
        if self.citations:
            lines += ["| # | Source type | Source | Quote or summary |", "|---|---|---|---|"]
            for index, citation in enumerate(self.citations, start=1):
                lines.append(
                    f"| {index} | {citation.source_type} | "
                    f"{_cell(citation.source_ref)} | {_cell(citation.quote_or_summary)} |"
                )
        else:
            lines.append("_No citations._")

        lines += ["", "## Confidence", "", f"{self.confidence:.2f}"]

        lines += ["", "## Tools used", ""]
        lines += [f"- `{tool}`" for tool in self.tools_used] or ["_None._"]

        lines += ["", "## Guardrail flags", ""]
        if self.guardrail_flags:
            lines += ["| Stage | Rule | Action | Detail |", "|---|---|---|---|"]
            for flag in self.guardrail_flags:
                lines.append(
                    f"| {flag.stage} | {_cell(flag.rule)} | {flag.action} | "
                    f"{_cell(flag.detail)} |"
                )
        else:
            lines.append("_None fired._")

        lines += ["", f"_Run ID: `{self.run_id}`_", ""]
        return "\n".join(lines)


def _cell(text: str) -> str:
    """Make text safe inside a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


# --- Section registry ------------------------------------------------------
# Sections holding a single model, and sections holding a list of them.
SECTION_MODELS: Final[dict[str, type[BaseModel]]] = {
    "task": Task,
    "plan": Plan,
    "analysis": Analysis,
    "draft": Draft,
    "critique": Critique,
    "final_report": FinalReport,
}

LIST_ITEM_MODELS: Final[dict[str, type[BaseModel]]] = {
    "evidence": EvidenceItem,
    "guardrail_flags": GuardrailFlag,
}

ALL_SECTIONS: Final[tuple[str, ...]] = (*SECTION_MODELS, *LIST_ITEM_MODELS)

LIST_ADAPTERS: Final[dict[str, TypeAdapter]] = {
    name: TypeAdapter(list[model]) for name, model in LIST_ITEM_MODELS.items()
}
