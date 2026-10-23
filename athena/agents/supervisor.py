"""supervisor: the orchestrator that runs the five stages in order.

Chosen mechanism: a small custom ``BaseAgent``. ``SequentialAgent`` is supported
by google-adk 2.12 and would run the same list, but the supervisor also has to
time each stage and hand back the final report, and a custom agent does both in
one place without a second wrapper. No ``transfer_to_agent`` anywhere: control
returns to this loop after every stage.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from athena.agents import bb_io
from athena.agents.a2a_stage import DEFAULT_CARD_URL, build_a2a_stage
from athena.agents.planner import build_planner
from athena.agents.researcher import build_researcher
from athena.blackboard import Blackboard


def stage_pause_seconds() -> float:
    """Gap between stages. Free tiers meter input tokens per minute; back-to-back
    stages exhaust that budget and make every provider call fail. 30 s is enough
    for Groq's 7000 ITPM free tier; set ATHENA_STAGE_PAUSE=0 on a paid tier."""
    return float(os.getenv("ATHENA_STAGE_PAUSE") or 30)


class SupervisorAgent(BaseAgent):
    """Runs planner -> researcher -> analyst -> writer -> critic, then reports."""

    stages: list[BaseAgent]
    run_id: str
    store: Any = None

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        pause = stage_pause_seconds()
        for index, stage in enumerate(self.stages):
            if index and pause > 0:
                await asyncio.sleep(pause)
            started = time.perf_counter()
            async for event in stage.run_async(ctx):
                yield event
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            yield Event(
                author=self.name,
                custom_metadata={"stage": stage.name, "stage_ms": elapsed_ms},
            )

        report = bb_io.section(Blackboard.open(self.run_id, self.store), "final_report")
        text = report.to_markdown() if report else "(no final report was written)"
        yield Event(
            author=self.name,
            content=types.Content(parts=[types.Part(text=text)]),
            custom_metadata={"final": True},
        )


def build_supervisor(
    run_id: str,
    store: Any = None,
    card_url: str = DEFAULT_CARD_URL,
) -> SupervisorAgent:
    """Build the pipeline for one run."""
    return SupervisorAgent(
        name="supervisor",
        description="Runs the sequential research pipeline over one blackboard run.",
        run_id=run_id,
        store=store,
        stages=[
            build_planner(run_id, store),
            build_researcher(run_id, store),
            build_a2a_stage("analyst", run_id, card_url, store),
            build_a2a_stage("writer", run_id, card_url, store),
            build_a2a_stage("critic", run_id, card_url, store),
        ],
    )
