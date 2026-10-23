"""researcher: an ADK LlmAgent with MCP tool servers attached.

Two shapes, same code: ``build_researcher`` gets all three servers (the
sequential baseline), ``build_role_researcher`` gets exactly one (the parallel
and hierarchical workers).

The model decides which tools to call. What it cannot do is decide what the
evidence says: every tool result is written by the ``evidence_recorder`` hook,
which reads the actual response.
"""

from __future__ import annotations

import sys
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

from athena.agents import bb_io
from athena.agents.evidence_recorder import (
    MAX_TOOL_CALLS,
    ToolBudget,
    make_evidence_recorder,
    make_tool_guard,
)
from athena.blackboard import Blackboard, BlackboardStore
from athena.config.llm import get_adk_model
from athena.tools.settings import REPO_ROOT

INSTRUCTION = """You are the researcher. Gather evidence for the task below.

Rules:
- Call `rag_search` at least once and at most twice, with top_k=3. Use a short,
  focused query (not the whole question verbatim).
- If the task involves numbers, arithmetic, totals or a trend, call `calculate`
  or `forecast` and put the exact figures in the expression/values.
- Only call `web_search` if the corpus cannot answer and the question needs
  current information.
- Do not answer the question and do not summarise. The evidence is already being
  recorded. When you have enough, reply with the single word DONE.
"""

ROLE_INSTRUCTIONS = {
    "rag_researcher": "Search the NASA document corpus. Call `rag_search` once "
    "with a short focused query (top_k=3). Ignore the other tools.",
    "web_researcher": "Search the web. Call `web_search` once (max_results=3) "
    "with a short query. Ignore the other tools.",
    "calc_researcher": "Work the numbers. Call `calculate` or `forecast` with "
    "the exact figures the task mentions. Ignore the other tools.",
}

SERVERS = {
    "rag": "athena.tools.rag_server",
    "web": "athena.tools.web_server",
    "calc": "athena.tools.calc_server",
}

ROLE_SERVER = {
    "rag_researcher": "rag",
    "web_researcher": "web",
    "calc_researcher": "calc",
}


def build_toolset(module: str, timeout: float = 120.0) -> MCPToolset:
    """One stdio MCP server as an ADK toolset."""
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", module],
                cwd=str(REPO_ROOT),
            ),
            timeout=timeout,
        )
    )


def build_researcher(
    run_id: str,
    store: BlackboardStore | None = None,
    task_text: str | None = None,
    sink: Any = None,
) -> LlmAgent:
    """The sequential researcher: all three tools, one agent."""
    text = task_text if task_text is not None else bb_io.task_text(
        Blackboard.open(run_id, store)
    )
    budget = ToolBudget(MAX_TOOL_CALLS)
    return LlmAgent(
        name="researcher",
        model=get_adk_model("researcher"),
        description="Gathers evidence with the RAG, web and calculator tools.",
        instruction=f"{INSTRUCTION}\nThe task:\n{text}\n",
        tools=[build_toolset(SERVERS[key]) for key in ("rag", "web", "calc")],
        before_tool_callback=make_tool_guard(budget),
        after_tool_callback=make_evidence_recorder(run_id, store, sink=sink),
    )


def build_role_researcher(
    role: str,
    run_id: str,
    store: BlackboardStore | None = None,
    task_text: str | None = None,
    sink: Any = None,
) -> LlmAgent:
    """A worker researcher with exactly one MCP server attached."""
    text = task_text if task_text is not None else bb_io.task_text(
        Blackboard.open(run_id, store)
    )
    budget = ToolBudget(MAX_TOOL_CALLS)
    return LlmAgent(
        name=role,
        model=get_adk_model("researcher"),
        description=f"The {role} worker.",
        instruction=f"You are the {role}. {ROLE_INSTRUCTIONS[role]}\n"
        f"The task:\n{text}\nReply DONE when finished.",
        tools=[build_toolset(SERVERS[ROLE_SERVER[role]])],
        before_tool_callback=make_tool_guard(budget),
        after_tool_callback=make_evidence_recorder(run_id, store, sink=sink),
    )


class AssignedWorker(BaseAgent):
    """Runs its worker only when the plan gives it a step.

    The plan does not exist when the graph is built, so the decision is deferred
    to run time and stays deterministic: a worker runs iff some step names it.
    """

    role: str
    worker: Any
    run_id: str
    store: Any = None

    async def _run_async_impl(self, ctx: InvocationContext):
        plan = bb_io.section(Blackboard.open(self.run_id, self.store), "plan")
        assigned = {step.assigned_to for step in plan.steps} if plan else set()
        if self.role not in assigned:
            yield Event(
                author=self.name,
                custom_metadata={"skipped": True, "reason": "no plan step"},
            )
            return
        async for event in self.worker.run_async(ctx):
            yield event
