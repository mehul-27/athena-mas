"""Stage 1: one CrewAI agent ("analyst") served over A2A on port 9001.

CrewAI 1.15.28 has A2A *client* support and Agent Card generation, but no HTTP
server, so the server half is the official ``a2a-sdk`` (AgentExecutor + its
FastAPI route helpers). CrewAI runs the agent inside that executor.

Run:  .venv\\Scripts\\python spike\\crew_a2a_server.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import uvicorn
from a2a.helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena.config.llm import get_crewai_llm  # noqa: E402

HOST, PORT = "127.0.0.1", 9001
BASE_URL = f"http://{HOST}:{PORT}"


def run_analyst(request_text: str) -> str:
    """Run the single-agent Crew synchronously and return its one-sentence reply."""
    from crewai import Agent, Crew, Process, Task

    analyst = Agent(
        role="Analyst",
        goal="Give one sharp, useful sentence of analysis on whatever you are asked.",
        backstory="You are a concise analyst: you read, decide what matters, and "
        "answer in exactly one sentence. You never pad or use bullet points.",
        llm=get_crewai_llm(),
        verbose=False,
    )
    task = Task(
        description="Analyse the following request and reply in one sentence."
        f"\n\nRequest: {request_text}",
        expected_output="One sentence of analysis, plain text, no preamble.",
        agent=analyst,
    )
    crew = Crew(agents=[analyst], tasks=[task], process=Process.sequential, verbose=False)
    return str(crew.kickoff()).strip()


class AnalystExecutor(AgentExecutor):
    """Bridges A2A requests to the CrewAI analyst."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # CrewAI's kickoff() is blocking, so keep it off the event loop.
        answer = await asyncio.to_thread(run_analyst, context.get_user_input())
        await event_queue.enqueue_event(
            new_text_message(
                answer, context_id=context.context_id, task_id=context.task_id
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("The analyst agent does not support cancellation.")


def build_agent_card() -> AgentCard:
    """The published card. A2A 1.0 keeps URLs inside supported_interfaces."""
    interfaces = [
        AgentInterface(url=BASE_URL, protocol_binding=kind, protocol_version="1.0")
        for kind in ("JSONRPC", "HTTP+JSON")
    ]
    return AgentCard(
        name="analyst",
        description="A CrewAI analyst agent. Send it any text; it replies with a "
        "one-sentence analysis.",
        version="1.0.0",
        supported_interfaces=interfaces,
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="analyse_text", name="Analyse text", tags=["analysis"],
                description="Returns one sentence of analysis about the given text.",
            )
        ],
    )


def create_app() -> FastAPI:
    card = build_agent_card()
    handler = DefaultRequestHandler(
        agent_executor=AnalystExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI(title="Athena analyst (A2A)")
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
        rest_routes=create_rest_routes(handler),
    )
    return app


if __name__ == "__main__":
    print(f"Agent Card: {BASE_URL}/.well-known/agent-card.json")
    uvicorn.run(create_app(), host=HOST, port=PORT, log_level="info")
