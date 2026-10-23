"""Minimal A2A client used by the ADK stage agents.

Only a run_id, a role and a short instruction travel over A2A. Evidence, drafts
and transcripts stay in the blackboard; the payload is capped well below the
600-byte budget the tests assert.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.helpers import get_stream_response_text, new_text_message
from a2a.types import Role, SendMessageRequest

INSTRUCTION_LIMIT = 300
MAX_PAYLOAD_BYTES = 600


def build_payload(run_id: str, role: str, instruction: str) -> str:
    """The exact JSON body sent to the CrewAI server."""
    payload = json.dumps(
        {
            "run_id": run_id,
            "role": role,
            "instruction": " ".join((instruction or "").split())[:INSTRUCTION_LIMIT],
        },
        separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:  # pragma: no cover - guarded by the cap
        raise ValueError(f"A2A payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    return payload


async def send_role_request(card_url: str, payload: str, timeout: float = 300.0) -> str:
    """Send one payload and return the server's short acknowledgement text."""
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
        return "".join(chunks).strip()
