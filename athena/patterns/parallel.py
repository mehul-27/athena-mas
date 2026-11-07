"""Parallel researchers: the three workers run at the same time.

Mechanism: bounded ``asyncio`` gathering inside a small ``BaseAgent``. ADK 2.12
still ships ``ParallelAgent``, but it is deprecated in favour of ``Workflow`` and
it insists on building its own branch contexts. The workers here already ignore
the parent context, and the supervisors yield a single ordered stream, so
gathering their event streams is simpler and avoids the deprecated class.

Lifecycle: every worker owns an MCP stdio toolset. A plain ``gather`` leaves the
siblings running when one worker raises, and leaves one tool-server subprocess
per worker alive after the stage returns, which is how a finished run could hang
with its summary already written. The pool therefore gathers with a deadline,
cancels survivors on any exit, and closes every worker's toolsets in ``finally``.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from athena.agents.a2a_stage import DEFAULT_CARD_URL, build_a2a_stage
from athena.agents.lifecycle import close_toolsets, pool_timeout_seconds, run_bounded
from athena.agents.planner import build_planner
from athena.agents.researcher import AssignedWorker, build_role_researcher
from athena.agents.supervisor import SupervisorAgent
from athena.patterns.recovery import build_recovery_review

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

        try:
            results = await run_bounded(
                [collect(worker) for worker in self.workers],
                timeout=pool_timeout_seconds(),
                label="research pool",
            )
            for events in results:
                for event in events:
                    yield event
        finally:
            # The stage owns these toolsets: no tool-server subprocess may
            # outlive it, on success, failure, cancellation or timeout.
            await close_toolsets(self.workers)


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
            build_recovery_review(run_id, store, card_url),
        ],
    )
