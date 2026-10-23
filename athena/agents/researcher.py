"""researcher: an ADK LlmAgent with the three MCP tool servers attached.

The model decides which tools to call. What it cannot do is decide what the
evidence says: every tool result is written to the blackboard by the
``evidence_recorder`` hook, which reads the actual response.
"""

from __future__ import annotations

import sys

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

from athena.agents import bb_io
from athena.agents.evidence_recorder import make_evidence_recorder
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

SERVERS = {
    "rag": "athena.tools.rag_server",
    "web": "athena.tools.web_server",
    "calc": "athena.tools.calc_server",
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


def build_researcher(run_id: str, store: BlackboardStore | None = None) -> LlmAgent:
    """The researcher agent, bound to one run."""
    blackboard = Blackboard.open(run_id, store)
    return LlmAgent(
        name="researcher",
        model=get_adk_model("researcher"),
        description="Gathers evidence with the RAG, web and calculator tools.",
        instruction=f"{INSTRUCTION}\nThe task:\n{bb_io.task_text(blackboard)}\n",
        tools=[
            build_toolset(SERVERS["rag"]),
            build_toolset(SERVERS["web"]),
            build_toolset(SERVERS["calc"]),
        ],
        after_tool_callback=make_evidence_recorder(run_id, store),
    )
