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
from athena.agents.budget import Budget, make_budget_callbacks
from athena.agents.evidence_recorder import make_evidence_recorder
from athena.agents.requirements import finding_requirement
from athena.agents.routing import source_guidance
from athena.agents.untrusted import UNTRUSTED_DATA_RULE
from athena.blackboard import Blackboard, BlackboardStore
from athena.config.llm import get_adk_model
from athena.observability.recorder import get_recorder
from athena.tools.settings import REPO_ROOT
from athena.tools.web.urls import plain_http_url

TOOL_DISCIPLINE = (
    "Call each search tool at most once per distinct query. Use `fetch_page` on "
    "relevant primary-source URLs; do not fetch mirrors or aggregators. Pass "
    "the URL as a plain HTTP(S) string, never a Markdown link. After a tool "
    "returns, its result is recorded automatically; do not repeat the call, and "
    "finish with a one-sentence summary. " + UNTRUSTED_DATA_RULE
)

INSTRUCTION = """You are the researcher. Gather evidence for the task below.

Rules:
- Choose sources to fit the question. For software, programming-language,
  library/API, release, standards, or current factual questions, start with
  `web_search` and fetch the relevant official/primary pages. Do not require a
  RAG call for every question.
- For questions about supplied local documents, use `rag_search` and base claims
  on the returned passages.
- Use `rag_search` as a supplementary source only when the corpus is plausibly
  relevant. If the corpus is unrelated, do not treat it as evidence.
- A search snippet is a discovery hint, not sufficient evidence for a specific
  claim. Fetch the page before relying on it.
- If the task involves numbers, arithmetic, totals or a trend, call `calculate`
  or `forecast` and put the exact figures in the expression/values.
- When a web result is a relevant primary source, call `fetch_page` on that URL
  before relying on it. Cite the fetched page text, not the search snippet.
- If a required finding still lacks a primary source, use the remaining fetch
  budget to find it; if it remains unavailable, record the gap instead of
  inventing evidence.
- Mirrors, reposts and aggregators are corroboration only; do not treat them as
  independent primary sources.
- Do not answer the question and do not summarise. The evidence is already being
  recorded. When you have enough, reply with the single word DONE.
"""

ROLE_INSTRUCTIONS = {
    "rag_researcher": "Search the NASA document corpus. Call `rag_search` once "
    "with a short focused query (top_k=3). Ignore the other tools.",
    "web_researcher": "Search the web with `web_search` once (max_results=3), "
    "then call `fetch_page` on the most relevant primary-source result(s). "
    "Ignore the NASA corpus and calculator tools.",
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


def plain_fetch_urls(callback):
    """Normalize fetch_page URLs before the MCP tool receives them."""
    def before_tool(tool, args, tool_context):
        if getattr(tool, "name", "") == "fetch_page" and isinstance(args, dict):
            args["url"] = plain_http_url(args.get("url"))
        return callback(tool, args, tool_context)

    return before_tool


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
    budget = Budget.from_env("researcher")
    before_model, before_tool = make_budget_callbacks(budget, get_recorder(), "researcher")
    before_tool = plain_fetch_urls(before_tool)
    return LlmAgent(
        name="researcher",
        model=get_adk_model("researcher"),
        description="Gathers evidence with the RAG, web and calculator tools.",
        instruction=(
            f"{INSTRUCTION}\n{TOOL_DISCIPLINE}\n{source_guidance(text)}\n"
            f"{finding_requirement(text)}\nThe task:\n{text}\n"
        ),
        tools=[build_toolset(SERVERS[key]) for key in ("rag", "web", "calc")],
        before_model_callback=before_model,
        before_tool_callback=before_tool,
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
    budget = Budget.from_env(role)
    before_model, before_tool = make_budget_callbacks(budget, get_recorder(), role)
    before_tool = plain_fetch_urls(before_tool)
    return LlmAgent(
        name=role,
        model=get_adk_model("researcher"),
        description=f"The {role} worker.",
        instruction=(
            f"You are the {role}. {ROLE_INSTRUCTIONS[role]}\n{TOOL_DISCIPLINE}\n"
            f"{source_guidance(text)}\n{finding_requirement(text)}\n"
            f"The task:\n{text}\nReply DONE when finished."
        ),
        tools=[build_toolset(SERVERS[ROLE_SERVER[role]])],
        before_model_callback=before_model,
        before_tool_callback=before_tool,
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
