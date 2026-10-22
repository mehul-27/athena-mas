"""Stage 2 + 3: the ADK "supervisor" agent.

Two different protocols are attached to the same root agent:

- ``analyst``: a CrewAI agent reached over **A2A** via ``RemoteA2aAgent``. It is
  never imported or called as a Python function; every request is an HTTP
  message/send to the Stage 1 server.
- ``add_numbers``: an **MCP** tool served by ``spike/mcp_server.py`` in its own
  stdio subprocess, attached through ADK's ``MCPToolset``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent  # noqa: E402
from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams  # noqa: E402
from mcp import StdioServerParameters  # noqa: E402

from athena.config.llm import get_adk_model  # noqa: E402

ANALYST_AGENT_CARD_URL = "http://127.0.0.1:9001/.well-known/agent-card.json"
MCP_SERVER_PATH = Path(__file__).with_name("mcp_server.py")

ANALYST_DESCRIPTION = (
    "An analyst running in a separate process on CrewAI, reached over A2A. "
    "Send it any text and it replies with a one-sentence analysis. Use it for "
    "commentary, opinions or interpretation of a fact, number or result."
)

SUPERVISOR_INSTRUCTION = """You are the Athena supervisor.

You have two capabilities:

- `add_numbers` (MCP tool): adds two numbers. ALWAYS use this tool for
  arithmetic. Never compute a sum in your head.
- `analyst` (A2A remote agent): a separate CrewAI process. Transfer to it when
  the request needs commentary, opinion or interpretation.

Rules:
- If a request needs both, use `add_numbers` first, then transfer to `analyst`
  and tell it the computed result so it can comment on it.
- Never invent the analyst's words. To get its opinion you must transfer.
"""


def build_analyst() -> RemoteA2aAgent:
    """The CrewAI analyst, reached over A2A rather than imported."""
    return RemoteA2aAgent(
        name="analyst",
        agent_card=ANALYST_AGENT_CARD_URL,
        description=ANALYST_DESCRIPTION,
    )


def build_mcp_tools() -> MCPToolset:
    """The MCP server as an ADK toolset, launched as a stdio subprocess."""
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=[str(MCP_SERVER_PATH)],
            ),
            timeout=30.0,
        )
    )


def build_supervisor() -> LlmAgent:
    """The ADK root agent: LLM + MCP tool + A2A sub-agent."""
    return LlmAgent(
        name="supervisor",
        model=get_adk_model(),
        description="Athena's root agent. Uses MCP tools and delegates over A2A.",
        instruction=SUPERVISOR_INSTRUCTION,
        tools=[build_mcp_tools()],
        sub_agents=[build_analyst()],
    )
