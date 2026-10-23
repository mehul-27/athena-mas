"""Minimal A2A client used by the ADK stage agents.

In blackboard mode the payload is only ``run_id`` + ``role`` + a short
instruction. In p2p mode it also carries a ``context`` string, because the
message is the only data path. Byte counters here feed the run summary.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.helpers import get_stream_response_text, new_text_message
from a2a.types import Role, SendMessageRequest

INSTRUCTION_LIMIT = 300
MAX_PAYLOAD_BYTES = 600
MAX_CONTEXT_CHARS = 6000

_STATS = {"out": 0, "in": 0}


def reset_stats() -> None:
    _STATS["out"] = 0
    _STATS["in"] = 0


def stats() -> dict[str, int]:
    return dict(_STATS)


def build_payload(run_id: str, role: str, instruction: str, context: str | None = None) -> str:
    """The exact JSON body sent to the CrewAI server."""
    body: dict[str, str] = {
        "run_id": run_id,
        "role": role,
        "instruction": " ".join((instruction or "").split())[:INSTRUCTION_LIMIT],
    }
    if context:
        body["context"] = context[:MAX_CONTEXT_CHARS]

    payload = json.dumps(body, separators=(",", ":"))
    if context is None and len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"blackboard-mode payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    return payload


def a2a_timeout() -> float:
    """A role can wait on the shared limiter before it answers; be patient."""
    return float(os.getenv("ATHENA_A2A_TIMEOUT") or 900)


async def send_role_request(card_url: str, payload: str, timeout: float | None = None) -> str:
    """Send one payload and return the server's acknowledgement text."""
    timeout = a2a_timeout() if timeout is None else timeout
    _STATS["out"] += len(payload.encode("utf-8"))

    parsed = urlparse(card_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    async with httpx.AsyncClient(timeout=timeout) as http:
        card = await A2ACardResolver(httpx_client=http, base_url=base_url).get_agent_card(
            relative_card_path=parsed.path
        )
        client = ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(card)

        request = SendMessageRequest()
        request.message.CopyFrom(new_text_message(payload, role=Role.ROLE_USER))

        chunks = []
        async for response in client.send_message(request):
            text = get_stream_response_text(response)
            if text:
                chunks.append(text)

    reply = "".join(chunks).strip()
    _STATS["in"] += len(reply.encode("utf-8"))
    return reply
