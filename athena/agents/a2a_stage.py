"""A2A stage agents: the ADK side of the analyst / writer / critic calls.

Each stage sends a run_id, its role and a short instruction. The work happens in
the CrewAI process, which reads and writes the blackboard itself; what comes
back is a one-line acknowledgement, never content.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from athena.agents.a2a_client import build_payload, send_role_request

DEFAULT_CARD_URL = "http://127.0.0.1:9001/.well-known/agent-card.json"

INSTRUCTIONS = {
    "analyst": "Read the task and the recorded evidence, then write the analysis section.",
    "writer": "Using the task, plan, evidence and analysis, write the draft and the final report.",
    "critic": "Review the draft, the final report and the evidence, then write the critique.",
}


class A2AStageAgent(BaseAgent):
    """Runs one CrewAI role over A2A and yields its acknowledgement."""

    role: str
    card_url: str = DEFAULT_CARD_URL
    instruction: str
    run_id: str
    store: Any = None

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        payload = build_payload(self.run_id, self.role, self.instruction)
        ack = await send_role_request(self.card_url, payload)
        yield Event(
            author=self.name,
            content=types.Content(parts=[types.Part(text=ack)]),
            custom_metadata={
                "a2a_role": self.role,
                "a2a_payload": payload,
                "a2a_payload_bytes": len(payload.encode("utf-8")),
            },
        )


def build_a2a_stage(
    role: str,
    run_id: str,
    card_url: str = DEFAULT_CARD_URL,
    store: Any = None,
) -> A2AStageAgent:
    """Build the stage agent for one CrewAI role."""
    return A2AStageAgent(
        name=role,
        role=role,
        run_id=run_id,
        store=store,
        card_url=card_url,
        instruction=INSTRUCTIONS[role],
        description=f"The CrewAI {role}, reached over A2A.",
    )
