"""The CrewAI A2A server: one endpoint, three roles.

The whole contract is the message body: ``{"run_id", "role", "instruction"}``.
The server reads inputs from the blackboard and replies with a short JSON
acknowledgement (section + version). No content ever travels back over A2A.

Run with ``python -m athena.agents.crew_server`` or via ``scripts/dev_up.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
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

if __package__ in (None, ""):  # allow `python athena/agents/crew_server.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from athena.agents import crew_roles
from athena.agents.crew_p2p import P2P_ROLES
from athena.agents.crew_writer import write_report
from athena.blackboard import Blackboard
from athena.config.llm import configure_process
from athena.observability.litellm_usage import install_usage_logger
from athena.observability.recorder import Recorder, set_recorder
from athena.observability.redact import redact

HOST, PORT = "127.0.0.1", 9001
BASE_URL = f"http://{HOST}:{PORT}"
ROLES = {
    "analyst": crew_roles.analyse,
    "writer": write_report,
    "critic": crew_roles.critique,
}


def _ack(**fields) -> str:
    return json.dumps(fields, separators=(",", ":"), default=str)


def run_role_request(text: str, roles: dict | None = None, store=None) -> str:
    """Parse one request, run the role, return a short acknowledgement.

    A body with ``context`` is a p2p request: the role works from the message
    alone and returns its output inline. Without ``context`` the role reads and
    writes the blackboard and returns only section + version.
    """
    try:
        body = json.loads(text)
    except ValueError:
        return _ack(ok=False, error="body must be JSON: {run_id, role, instruction}")
    if not isinstance(body, dict):
        return _ack(ok=False, error="body must be a JSON object")

    run_id = str(body.get("run_id") or "").strip()
    role = str(body.get("role") or "").strip().lower()
    instruction = str(body.get("instruction") or "").strip()
    p2p = "context" in body
    context = str(body.get("context") or "")
    roles = roles if roles is not None else (P2P_ROLES if p2p else ROLES)

    if not run_id:
        return _ack(ok=False, error="run_id is required")
    handler = roles.get(role)
    if handler is None:
        return _ack(ok=False, error=f"unknown role {role!r}; expected {sorted(roles)}")
    if not p2p and Blackboard.open(run_id, store).read("task") is None:
        return _ack(ok=False, error=f"unknown run_id {run_id!r}")

    recorder = Recorder.for_run(run_id)
    recorder.emit("stage_start", role, "crewai")
    previous = set_recorder(recorder)
    started = time.perf_counter()
    try:
        if p2p:
            result = handler(run_id, instruction, store=None, context=context)
        else:
            result = handler(run_id, instruction, store=store)
    except Exception as exc:  # noqa: BLE001 - a bad generation is an ack, not a crash
        error_type = type(exc).__name__
        message = (redact(str(exc)) or "")[:200]
        recorder.emit(
            "error", role, "crewai", ok=False,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            error_type=error_type, error_message=message,
        )
        # The ADK side decides what to tell the user from these two fields.
        return _ack(ok=False, role=role, error_type=error_type, error=message)
    finally:
        set_recorder(previous)
    recorder.emit(
        "stage_end", role, "crewai",
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
        tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"),
        extra={"reasoning_tokens": result.get("reasoning_tokens"),
               "calls": result.get("calls")},
    )

    ack: dict = {"ok": True, "role": role}
    if "section" in result:
        ack["section"] = result["section"]
    if "version" in result:
        ack["version"] = result["version"]
    if "content" in result:
        ack["content"] = result["content"]
    for key in ("tokens_in", "tokens_out", "reasoning_tokens", "calls", "model_ms"):
        if result.get(key) is not None:
            ack[key] = result[key]
    if result.get("fallback"):
        ack["fallback"] = True
        ack["note"] = result.get("note", "generation fallback")
    return _ack(**ack)


class CrewA2AExecutor(AgentExecutor):
    """Dispatches an A2A message to one CrewAI role."""

    def __init__(self, roles: dict | None = None, store=None) -> None:
        self.roles = roles if roles is not None else ROLES
        self.store = store

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # CrewAI is blocking; keep it off the event loop.
        ack = await asyncio.to_thread(
            run_role_request, context.get_user_input(), self.roles, self.store
        )
        await event_queue.enqueue_event(
            new_text_message(ack, context_id=context.context_id, task_id=context.task_id)
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("The crew server does not support cancellation.")


def build_agent_card() -> AgentCard:
    interfaces = [
        AgentInterface(url=BASE_URL, protocol_binding=kind, protocol_version="1.0")
        for kind in ("JSONRPC", "HTTP+JSON")
    ]
    return AgentCard(
        name="crewai-team",
        description="Athena's CrewAI team: analyst, writer and critic, addressed by role.",
        version="1.0.0",
        supported_interfaces=interfaces,
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(id=role, name=role.title(), tags=["crewai", role],
                       description=f"Runs the {role} role against one blackboard run.")
            for role in ROLES
        ],
    )


def create_app(roles: dict | None = None, store=None) -> FastAPI:
    configure_process()  # export the endpoint and arm the guard
    # CrewAI calls end in litellm, so usage is captured there, not in CrewOutput.
    install_usage_logger()
    card = build_agent_card()
    handler = DefaultRequestHandler(
        agent_executor=CrewA2AExecutor(roles, store),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI(title="Athena CrewAI team (A2A)")
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
