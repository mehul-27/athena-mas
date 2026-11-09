"""The CrewAI A2A server: one endpoint, three roles.

The whole contract is the message body: ``{"run_id", "role", "instruction"}``.
The server reads inputs from the blackboard and replies with a short JSON
acknowledgement (section + version). No content ever travels back over A2A.

When the deployment sets ``ATHENA_A2A_SHARED_TOKEN``, a body carrying an HMAC is
verified before any role runs: a forged or cross-run message is refused with an
acknowledgement instead of being dispatched. With no token configured the
single-host flow is unchanged (see :func:`peer_auth_error`).

Run with ``python -m athena.agents.crew_server`` or via ``scripts/dev_up.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
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
from athena.guardrails.interagent import (
    REQUIRE_SIG_ENV,
    SIG_FIELD,
    TOKEN_ENV,
    PeerAuth,
    emit_rejection,
    validate_message,
)
from athena.observability.guardrail_events import record_decision
from athena.observability.usage_hook import callback_count, install_usage_logger
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


def peer_auth_error(body: dict) -> str | None:
    """Why this body's signature is unacceptable, or None to accept it.

    Three cases, in order:

    * no shared token configured — nothing to verify against, so an unsigned
      local-dev flow behaves exactly as it did before;
    * a signature is present — it must verify against this process's token,
      otherwise the body was forged or signed for a different deployment;
    * no signature but a token is configured — allowed (a warning-only
      downgrade) unless ``ATHENA_A2A_REQUIRE_SIG=1`` turns it into an error.
    """
    if not (os.getenv(TOKEN_ENV) or "").strip():
        return None
    auth = PeerAuth()
    signature = body.get(SIG_FIELD)
    if signature:
        if auth.verify_message(body):
            return None
        return "signature mismatch: forged body or a different shared token"
    if auth.require_signature:
        return f"signature required ({REQUIRE_SIG_ENV}=1)"
    return None


def _rejection_run(claimed: str, store) -> tuple[str | None, str]:
    """``(run_id, source)`` an A2A rejection may be attributed to.

    The message's own ``run_id`` is attacker-controlled, so it is used **only
    when the blackboard confirms that run exists** (p2p requests have no
    blackboard, so they are never attributed from the message). Otherwise the
    row goes to whoever is recording this process — which, in the server, is the
    turn's own recorder if there is one — and ``source`` says which of the two
    happened so a reader can tell.
    """
    if claimed:
        try:
            if Blackboard.open(claimed, store).read("task") is not None:
                return claimed, "message_run_id_verified_on_blackboard"
        except Exception:  # noqa: BLE001 - an unreadable board means "not verified"
            pass
    return None, "refused_no_verified_run"


def _record_rejection(
    *,
    rule: str,
    action: str,
    detail: str,
    audit_run_id: str | None,
    run_id_source: str,
) -> None:
    """One ``interagent`` guardrail row for an A2A refusal. Never raises."""
    record_decision(
        stage="interagent",
        rule=rule,
        action=action,
        detail=detail,
        screen="crew_server.peer_auth_error",
        pipeline_stage="interagent",
        run_id=audit_run_id,
        agent="guardrails",
        framework="crewai",
        extra={"run_id_source": run_id_source},
    )


def _emit_verdict(verdict, audit_run_id: str | None, run_id_source: str) -> None:
    """Write rejected decisions and accepted warnings with trusted attribution."""
    emit_rejection(
        verdict,
        audit_run_id=audit_run_id,
        agent="guardrails",
        framework="crewai",
        run_id_source=run_id_source,
    )


def run_role_request(text: str, roles: dict | None = None, store=None) -> str:
    """Parse one request, run the role, return a short acknowledgement.

    A body with ``context`` is a p2p request: the role works from the message
    alone and returns its output inline. Without ``context`` the role reads and
    writes the blackboard and returns only section + version.

    Two independent controls run before any role is dispatched:

    * :func:`peer_auth_error` — the HMAC, with this server's own wording;
    * :func:`guardrails.interagent.validate_message` — the mode enum, the role
      allowlist, the field allowlist and the instruction/context size bounds.
      Its signature check is skipped (``check_signature=False``) because the HMAC
      was already decided above; everything else applies.

    A refusal from either one is returned as an acknowledgement and written as
    ``interagent`` ``guardrail`` rows. Attribution never uses the ``run_id`` the
    message claims: rows go to the run this process is recording, or to the
    message's run id only after the blackboard confirms that run exists. A
    message naming a run that does not exist is attributed to nobody.
    """
    try:
        body = json.loads(text)
    except ValueError:
        return _ack(ok=False, error="body must be JSON: {run_id, role, instruction}")
    if not isinstance(body, dict):
        return _ack(ok=False, error="body must be a JSON object")

    claimed = str(body.get("run_id") or "").strip()
    p2p = "context" in body
    mode = "p2p" if p2p else "blackboard"
    roles = roles if roles is not None else (P2P_ROLES if p2p else ROLES)
    audit_run, audit_source = _rejection_run(claimed, store)

    forged = peer_auth_error(body)
    if forged is not None:
        _record_rejection(
            rule="interagent.signature", action="blocked", detail=forged,
            audit_run_id=audit_run, run_id_source=audit_source,
        )
        return _ack(ok=False, error=forged)

    # The HMAC was decided above, so only the shape half runs here: mode, role
    # allowlist, field allowlist and the instruction/context size bounds.
    verdict = validate_message(
        body, roles, mode=mode, check_signature=False, emit=False,
        audit_run_id=audit_run, run_id_source=audit_source,
    )
    _emit_verdict(verdict, audit_run, audit_source)
    role = str(body.get("role") or "").strip().lower()
    if not claimed:
        return _ack(ok=False, error="run_id is required")
    handler = roles.get(role)
    if handler is None:
        return _ack(ok=False, error=f"unknown role {role!r}; expected {sorted(roles)}")
    if not verdict.ok:
        return _ack(ok=False, error="; ".join(verdict.errors[:3]))

    run_id = claimed
    instruction = str(body.get("instruction") or "").strip()
    context = str(body.get("context") or "")

    if not p2p and Blackboard.open(run_id, store).read("task") is None:
        return _ack(ok=False, error=f"unknown run_id {run_id!r}")

    recorder = Recorder.for_run(run_id)
    recorder.emit(
        "stage_start",
        role,
        "crewai",
        extra={"stage_scope": "crewai-remote", "handoff": "a2a"},
    )
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
        # A failed CrewAI role still owns a started stage. Terminalize it before
        # returning the acknowledgement; otherwise the trace keeps a phantom
        # "running" analyst/writer/critic row.
        recorder.emit(
            "stage_end", role, "crewai", ok=False,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            error_type=error_type, error_message=message,
            extra={"stage_scope": "crewai-remote", "terminal": "failure",
                   "handoff": "a2a"},
        )
        # The ADK side decides what to tell the user from these two fields.
        return _ack(ok=False, role=role, error_type=error_type, error=message)
    finally:
        set_recorder(previous)
    recorder.emit(
        "stage_end", role, "crewai",
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
        tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"),
        ok=True,
        extra={"reasoning_tokens": result.get("reasoning_tokens"),
               "calls": result.get("calls"),
               "terminal": "success",
               "stage_scope": "crewai-remote",
               "handoff": "a2a"},
    )

    ack: dict = {"ok": True, "role": role}
    if "section" in result:
        ack["section"] = result["section"]
    if "version" in result:
        ack["version"] = result["version"]
    if "content" in result:
        ack["content"] = result["content"]
    for key in ("tokens_in", "tokens_out", "reasoning_tokens", "calls", "model_ms", "estimated"):
        if result.get(key) is not None:
            ack[key] = result[key]
    if result.get("fallback"):
        ack["fallback"] = True
        ack["note"] = result.get("note", "generation fallback")
    return _ack(**ack)


class CrewA2AExecutor(AgentExecutor):
    """Dispatches an A2A message to one CrewAI role."""

    def __init__(self, roles: dict | None = None, store=None) -> None:
        # Keep None: run_role_request picks ROLES or P2P_ROLES from the body.
        # Defaulting here to ROLES is what sent a p2p request to analyse(),
        # which does not take a context argument.
        self.roles = roles
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
    # Safe startup identity: never print a key. Knowing which model/base this
    # process actually loaded is essential when ADK and CrewAI are separate
    # processes with different environment inheritance.
    print(
        "crew model:",
        os.getenv("LLM_MODEL") or "(default)",
        "| api host:",
        os.getenv("LLM_API_BASE") or "(provider default)",
        flush=True,
    )
    # CrewAI calls end in litellm, so usage is captured there, not in CrewOutput.
    install_usage_logger()
    print(f"usage logger installed (callbacks: {callback_count()})")
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
