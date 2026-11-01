"""Regression tests for source-role selection and duplicate plan dispatch."""

from __future__ import annotations

import asyncio

from athena.agents.routing import ensure_step_texts
from athena.blackboard import (
    Blackboard,
    Critique,
    Plan,
    PlanStep,
    SQLiteStore,
    Task,
)
from athena.patterns.hierarchical import HierarchicalAgent


class StubStage:
    def __init__(self, name, fn=None):
        self.name = name
        self.fn = fn
        self.calls = 0

    async def run_async(self, ctx):
        self.calls += 1
        if self.fn:
            self.fn(self.calls)
        yield None



def test_external_question_drops_unjustified_rag_step() -> None:
    steps = [
        "web_researcher: search official release notes",
        "rag_researcher: search the local corpus",
        "calc_researcher: verify numbers",
    ]

    selected = ensure_step_texts(
        steps,
        "What are two important changes in Python 3.13? Cite official sources.",
    )

    assert any(step.startswith("web_researcher:") for step in selected)
    assert not any(step.startswith("rag_researcher:") for step in selected)


def test_local_document_question_keeps_its_rag_step() -> None:
    steps = [
        "web_researcher: search for supplementary material",
        "rag_researcher: search the supplied handbook",
    ]

    selected = ensure_step_texts(
        steps,
        "According to the supplied handbook, what does configuration management cover?",
    )

    assert any(step.startswith("rag_researcher:") for step in selected)


def test_duplicate_web_plan_steps_dispatch_one_worker_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="external question"), store=store)

    def write_plan(_=None):
        blackboard.write(
            "plan",
            Plan(steps=[
                PlanStep(id="s1", description="search", assigned_to="web_researcher"),
                PlanStep(id="s2", description="fetch", assigned_to="web_researcher"),
            ]),
            author="planner",
        )

    def write_critique(_number):
        blackboard.write(
            "critique",
            Critique(issues=[], severity="low", approved=True),
            author="critic",
        )

    workers = {
        role: StubStage(role)
        for role in ("rag_researcher", "web_researcher", "calc_researcher")
    }
    agent = HierarchicalAgent(
        name="supervisor",
        planner=StubStage("planner", write_plan),
        workers=workers,
        analyst=StubStage("analyst"),
        writer=StubStage("writer"),
        critic=StubStage("critic", write_critique),
        run_id=blackboard.run_id,
        store=store,
        revision_limit=1,
    )

    asyncio.run(_collect_async(agent))

    assert workers["web_researcher"].calls == 1


async def _collect_async(agent):
    return [event async for event in agent._run_async_impl(None)]
