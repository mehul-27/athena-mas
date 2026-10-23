"""Supervisor agents: run a list of stages in order and hand back the report.

Chosen mechanism: a small custom ``BaseAgent``. ``SequentialAgent`` is supported
by google-adk 2.12 and would run the same list, but the supervisor also has to
time each stage and read the final report, and a custom agent does both in one
place. No ``transfer_to_agent`` anywhere: control returns to this loop after
every stage.
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
from athena.blackboard import Blackboard


def stage_pause_seconds() -> float:
    """Optional fixed gap between stages. Defaults to 0: the shared token
    limiter (athena/config/ratelimit.py) now paces both processes, so a sleep is
    only a fallback for providers without a token meter."""
    return float(os.getenv("ATHENA_STAGE_PAUSE") or 0)


async def timed_stage(
    supervisor: str, stage: BaseAgent, ctx: InvocationContext
) -> AsyncGenerator[Event, None]:
    """Run one stage, then emit a timing event carrying its name and duration."""
    started = time.perf_counter()
    async for event in stage.run_async(ctx):
        yield event
    yield Event(
        author=supervisor,
        custom_metadata={
            "stage": stage.name,
            "stage_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )


class SupervisorAgent(BaseAgent):
    """Runs its stages in order, then returns the final report."""

    stages: list[Any]  # real stages are BaseAgents; tests pass duck-typed stubs
    run_id: str
    store: Any = None

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        pause = stage_pause_seconds()
        for index, stage in enumerate(self.stages):
            if index and pause > 0:
                await asyncio.sleep(pause)
            async for event in timed_stage(self.name, stage, ctx):
                yield event

        report = bb_io.section(Blackboard.open(self.run_id, self.store), "final_report")
        text = report.to_markdown() if report else "(no final report was written)"
        yield Event(
            author=self.name,
            content=types.Content(parts=[types.Part(text=text)]),
            custom_metadata={"final": True},
        )


def build_supervisor(
    run_id: str,
    *,
    pattern: str = "sequential",
    store: Any = None,
    card_url: str | None = None,
):
    """Build the supervisor for a pattern. See ``athena.patterns``."""
    from athena.agents.a2a_stage import DEFAULT_CARD_URL
    from athena.patterns import UnknownPatternError, build_pattern

    return build_pattern(pattern, run_id, store, card_url or DEFAULT_CARD_URL)


__all__ = [
    "SupervisorAgent",
    "build_supervisor",
    "timed_stage",
    "UnknownPatternError",
]
