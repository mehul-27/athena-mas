"""Hierarchical: the supervisor is the manager, the plan says who works.

Dispatch is deterministic — the plan's ``assigned_to`` picks the worker. The
only LLM decision is the planner's, which is where it belongs. After the critic
rejects with severity ``high``, the supervisor re-runs the writer (which reads
the critique from the blackboard) and then the critic again.
"""

from __future__ import annotations

import os
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from athena.agents import bb_io
from athena.agents.researcher import build_role_researcher
from athena.agents.routing import classify, preferred_roles
from athena.agents.sufficiency import assess
from athena.agents.supervisor import timed_stage
from athena.agents.tool_report import build_tool_report
from athena.blackboard import Blackboard
from athena.observability.recorder import get_recorder
from athena.tools import registry

DEFAULT_MAX_REVISIONS = 1
FALLBACK_WORKER = "rag_researcher"


def max_revisions() -> int:
    return int(os.getenv("ATHENA_MAX_REVISIONS") or DEFAULT_MAX_REVISIONS)


class HierarchicalAgent(BaseAgent):
    """Manager agent: plan -> workers -> analyst -> (writer -> critic)*."""

    planner: Any
    workers: dict[str, Any]
    analyst: Any
    writer: Any
    critic: Any
    run_id: str
    store: Any = None
    revision_limit: int = DEFAULT_MAX_REVISIONS

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        async for event in timed_stage(self.name, self.planner, ctx):
            yield event

        blackboard = Blackboard.open(self.run_id, self.store)
        selection = registry.task_selection(self.run_id, self.store)
        available = registry.available_roles(selection)
        plan = bb_io.section(blackboard, "plan")
        steps = plan.steps if plan else []
        if not steps:
            task = bb_io.section(blackboard, "task")
            steps_text = preferred_roles(task.question if task else "", available)
        else:
            # The supervisor dispatches by role, not by step description. Running
            # the same worker twice for two steps therefore repeats the same task
            # and cannot target the second step's objective.
            steps_text = list(dict.fromkeys(step.assigned_to for step in steps))

        for role in steps_text:
            if role not in available:
                reason = "tools disabled by the user"
                self._note_skip(role, reason)
                yield Event(
                    author=self.name,
                    custom_metadata={"skipped": True, "role": role, "reason": reason},
                )
                continue
            worker = self.workers.get(role)
            if worker is None:
                continue
            async for event in timed_stage(self.name, worker, ctx):
                yield event

        async for event in timed_stage(self.name, self.analyst, ctx):
            yield event

        revisions = 0
        recoveries = 0
        recovery_limit = max(1, self.revision_limit)
        while True:
            async for event in timed_stage(self.name, self.writer, ctx):
                yield event
            async for event in timed_stage(self.name, self.critic, ctx):
                yield event

            critique = bb_io.section(blackboard, "critique")
            rejected = critique is not None and not critique.approved
            task = bb_io.section(blackboard, "task")
            tool_report = build_tool_report(self.run_id, self.store)
            assessment = assess(
                task.question if task else "",
                bb_io.load_evidence(blackboard, limit=None),
                enabled_tools=task.tools if task else None,
                tool_report=tool_report,
            )

            if not rejected and assessment.answer_as_asked:
                break

            missing_subject = bool(assessment.missing_subjects)
            missing_aspect = bool(assessment.missing_aspects)
            evidence_gap = missing_subject or missing_aspect or not assessment.ok
            if evidence_gap and recoveries < recovery_limit:
                recoveries += 1
                issues = "; ".join(assessment.issues)
                missing = set(assessment.missing_tools)
                route = classify(task.question if task else "")
                if route.kind == "local":
                    role = "rag_researcher"
                elif missing_subject or "fetch_page" in missing or assessment.required_urls:
                    role = "web_researcher"
                elif "rag_search" in missing and "rag_researcher" in available:
                    role = "rag_researcher"
                else:
                    role = "web_researcher"
                if role not in available:
                    self._note_recovery(
                        skipped=True,
                        role=role,
                        reason="tools disabled by the user",
                    )
                    break
                recovery_task = (
                    f"{bb_io.task_text(blackboard)}\n\n"
                    f"MISSING SUBJECTS: {', '.join(assessment.missing_subjects) or '(none)'}\n"
                    f"MISSING ANSWER PARTS: {', '.join(assessment.missing_aspects) or '(none)'}\n"
                    f"BLOCKING EVIDENCE ISSUES:\n{issues or '(none)'}\n\n"
                    f"CRITIC GAPS TO RESOLVE WITH NEW PRIMARY EVIDENCE:\n"
                    f"{bb_io.critique_text(blackboard)}\n\n"
                    "Target only these gaps. Do not repeat successful calls. Fetch only "
                    "missing or failed URLs, and preserve the evidence already recorded."
                )
                self._note_recovery(
                    skipped=False,
                    role=role,
                    reason=(
                        ("missing subjects: " + ", ".join(assessment.missing_subjects))
                        if missing_subject
                        else ("missing parts: " + ", ".join(assessment.missing_aspects))
                        if missing_aspect
                        else (issues or "critic requested stronger evidence")
                    ),
                )
                worker = build_role_researcher(
                    role, self.run_id, self.store, task_text=recovery_task
                )
                async for event in timed_stage(self.name, worker, ctx):
                    yield event
                continue

            # Evidence is sufficient for the asked subject; a high rejection is
            # now a prose revision, not a reason to repeat research.
            if rejected and critique.severity == "high" and revisions < self.revision_limit:
                revisions += 1
                self._note_recovery(
                    skipped=True,
                    reason="existing evidence is sufficient; revise with recorded evidence",
                )
                continue
            break

        yield Event(
            author=self.name,
            custom_metadata={
                "revision_rounds": revisions,
                "recovery_rounds": recoveries,
                "workers": steps_text,
            },
        )

        report = bb_io.section(blackboard, "final_report")
        text = report.to_markdown() if report else "(no final report was written)"
        yield Event(
            author=self.name,
            content=types.Content(parts=[types.Part(text=text)]),
            custom_metadata={"final": True},
        )

    def _note_recovery(
        self, *, skipped: bool, reason: str, role: str = ""
    ) -> None:
        recorder = get_recorder()
        if recorder is None:
            return
        recorder.emit(
            "recovery",
            "supervisor",
            "adk",
            payload_summary=(
                ("research recovery skipped: " if skipped else "research recovery: ")
                + (f"{role} " if role else "")
                + reason
            ),
            extra={
                "skipped": skipped,
                "role": role,
                "reason": reason,
            },
        )

    def _note_skip(self, role: str, reason: str) -> None:
        recorder = get_recorder()
        if recorder is None:
            return
        recorder.emit(
            "skip",
            role,
            "adk",
            payload_summary=f"{role} skipped: {reason}",
            extra={"reason": reason, "role": role},
        )


def build(run_id: str, store: Any = None, card_url: str = "") -> HierarchicalAgent:
    from athena.agents.a2a_stage import DEFAULT_CARD_URL, build_a2a_stage
    from athena.agents.planner import build_planner

    card_url = card_url or DEFAULT_CARD_URL
    return HierarchicalAgent(
        name="supervisor",
        description="Hierarchical pipeline: plan-driven dispatch with a revision loop.",
        planner=build_planner(run_id, store),
        workers={
            role: build_role_researcher(role, run_id, store)
            for role in ("rag_researcher", "web_researcher", "calc_researcher")
        },
        analyst=build_a2a_stage("analyst", run_id, card_url, store),
        writer=build_a2a_stage("writer", run_id, card_url, store),
        critic=build_a2a_stage("critic", run_id, card_url, store),
        run_id=run_id,
        store=store,
        revision_limit=max_revisions(),
    )
