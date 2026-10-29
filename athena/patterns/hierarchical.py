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
from athena.agents.routing import classify, critique_needs_research
from athena.agents.supervisor import timed_stage
from athena.blackboard import Blackboard

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
        plan = bb_io.section(blackboard, "plan")
        steps = plan.steps if plan else []
        if not steps:
            steps_text = [FALLBACK_WORKER]
        else:
            steps_text = [step.assigned_to for step in steps]

        for role in steps_text:
            worker = self.workers.get(role) or self.workers[FALLBACK_WORKER]
            async for event in timed_stage(self.name, worker, ctx):
                yield event

        async for event in timed_stage(self.name, self.analyst, ctx):
            yield event

        rounds = 0
        while True:
            async for event in timed_stage(self.name, self.writer, ctx):
                yield event
            async for event in timed_stage(self.name, self.critic, ctx):
                yield event

            critique = bb_io.section(blackboard, "critique")
            rejected = critique is not None and not critique.approved
            if not rejected or critique.severity != "high" or rounds >= self.revision_limit:
                break
            rounds += 1
            if critique_needs_research(list(critique.issues or [])):
                task = bb_io.section(blackboard, "task")
                gap_text = bb_io.critique_text(blackboard)
                recovery_task = (
                    f"{bb_io.task_text(blackboard)}\n\n"
                    f"CRITIC GAPS TO RESOLVE WITH NEW PRIMARY EVIDENCE:\n{gap_text}"
                )
                role = "rag_researcher" if classify(
                    task.question if task else ""
                ).kind == "local" else "web_researcher"
                worker = build_role_researcher(
                    role, self.run_id, self.store, task_text=recovery_task
                )
                async for event in timed_stage(self.name, worker, ctx):
                    yield event

        yield Event(
            author=self.name,
            custom_metadata={"revision_rounds": rounds, "workers": steps_text},
        )

        report = bb_io.section(blackboard, "final_report")
        text = report.to_markdown() if report else "(no final report was written)"
        yield Event(
            author=self.name,
            content=types.Content(parts=[types.Part(text=text)]),
            custom_metadata={"final": True},
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
