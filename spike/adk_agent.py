"""Stage 2: the ADK "supervisor" that delegates to the CrewAI analyst over A2A.

The analyst is *not* imported or called as a Python function here. It is a
``RemoteA2aAgent`` bound to the Stage 1 Agent Card URL, so every request the
supervisor makes to it is a real A2A message/send over HTTP.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent  # noqa: E402

from athena.config.llm import get_adk_model  # noqa: E402

ANALYST_AGENT_CARD_URL = "http://127.0.0.1:9001/.well-known/agent-card.json"

ANALYST_DESCRIPTION = (
    "An analyst running in a separate process on CrewAI. Send it any text and "
    "it replies with a one-sentence analysis. Use it for commentary, opinions "
    "or interpretation of a fact, number or result."
)

SUPERVISOR_INSTRUCTION = """You are the Athena supervisor.

You have one specialist agent, `analyst`, reachable over the A2A protocol.

Rules:
- If the request asks for commentary, opinion or interpretation, transfer to
  `analyst` and let it answer.
- Never invent the analyst's words. If you need its opinion, you must transfer.
- After the analyst replies, relay its analysis to the user as the final answer.
- Answer directly only for simple arithmetic or formatting the user asked for
  yourself.
"""


def build_analyst() -> RemoteA2aAgent:
    """The CrewAI analyst, reached over A2A rather than imported."""
    return RemoteA2aAgent(
        name="analyst",
        agent_card=ANALYST_AGENT_CARD_URL,
        description=ANALYST_DESCRIPTION,
    )


def build_supervisor() -> LlmAgent:
    """The ADK root agent: an LLM that transfers to the A2A analyst."""
    return LlmAgent(
        name="supervisor",
        model=get_adk_model(),
        description="Athena's root agent. Coordinates the analyst over A2A.",
        instruction=SUPERVISOR_INSTRUCTION,
        sub_agents=[build_analyst()],
    )
