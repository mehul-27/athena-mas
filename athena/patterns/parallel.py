"""Parallel researchers: the three workers run at the same time.

Mechanism: ``asyncio.gather`` inside a small ``BaseAgent``. ADK 2.12 still ships
``ParallelAgent``, but it is deprecated in favour of ``Workflow`` and it insists
on building its own branch contexts. The workers here already ignore the parent
context, and the supervisors yield a single ordered stream, so gathering their
event streams is simpler and avoids the deprecated class.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from athena.agents.a2a_stage import DEFAULT_CARD_URL, build_a2a_stage
from athena.agents.planner import build_planner
from athena.agents.researcher import AssignedWorker, build_role_researcher
from athena.agents.supervisor import SupervisorAgent

WORKERS = ("rag_researcher", "web_researcher", "calc_researcher")


class ConcurrentWorkers(BaseAgent):
    """Runs every worker concurrently and yields their events in order."""

    workers: list[Any]

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        async def collect(agent) -> list[Event]:
            events = []
            async for event in agent.run_async(ctx):
                events.append(event)
            return events

        results = await asyncio_gather([collect(worker) for worker in self.workers])
        for events in results:
            for event in events:
                yield event


def asyncio_gather(coroutines):
    import asyncio

    return asyncio.gather(*coroutines)


def build_research_pool(run_id: str, store: Any = None) -> ConcurrentWorkers:
    """One worker per tool, each skipped when the plan does not name it."""
    return ConcurrentWorkers(
        name="research_pool",
        description="Runs the assigned researchers concurrently.",
        workers=[
            AssignedWorker(
                name=role,
                role=role,
                worker=build_role_researcher(role, run_id, store),
                run_id=run_id,
                store=store,
            )
            for role in WORKERS
        ],
    )


def build(run_id: str, store: Any = None, card_url: str = DEFAULT_CARD_URL) -> SupervisorAgent:
    return SupervisorAgent(
        name="supervisor",
        description="Parallel-research pipeline over one blackboard run.",
        run_id=run_id,
        store=store,
        stages=[
            build_planner(run_id, store),
            build_research_pool(run_id, store),
            build_a2a_stage("analyst", run_id, card_url, store),
            build_a2a_stage("writer", run_id, card_url, store),
            build_a2a_stage("critic", run_id, card_url, store),
        ],
    )
