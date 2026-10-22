"""Offline tests for the Stage 1-3 spike.

Default run (`python -m pytest`) stays offline: it starts the A2A server
in-process and only fetches the published Agent Card - no LLM is called.
Anything that needs a live model or a live server is marked `e2e`.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

CARD_PATH = "/.well-known/agent-card.json"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def card_url() -> str:
    """Serve the A2A app on a free port and return the Agent Card URL."""
    import spike.crew_a2a_server as server_module

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    # The card publishes this URL, so it has to point at the port we bind.
    server_module.BASE_URL = base_url

    app = server_module.create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = base_url + CARD_PATH
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        server.should_exit = True
        pytest.fail("A2A server did not start within 30s")

    yield url

    server.should_exit = True
    thread.join(timeout=10)


def test_agent_card_has_name_and_url(card_url: str) -> None:
    """The published card names the agent and says where to reach it."""
    response = httpx.get(card_url, timeout=10)
    assert response.status_code == 200
    card = response.json()

    assert card["name"]
    # A2A 1.0 (a2a-sdk 1.x) moved the endpoint out of the top-level `url`
    # field into supportedInterfaces[]; accept either shape.
    urls = [iface["url"] for iface in card.get("supportedInterfaces", [])]
    if not urls and card.get("url"):
        urls = [card["url"]]
    assert urls, "Agent Card exposes no agent URL"
    assert all(url.startswith("http") for url in urls)


def test_agent_card_skills_present(card_url: str) -> None:
    """The card advertises at least one skill, as a client would expect."""
    card = httpx.get(card_url, timeout=10).json()
    assert card.get("skills")


def test_mcp_add_numbers() -> None:
    """The MCP tool's function is importable and does what it claims.

    Importing the server module must not start a server: the stdio loop lives
    behind ``if __name__ == "__main__"``.
    """
    from spike.mcp_server import add_numbers

    assert add_numbers(2, 3) == 5
    assert add_numbers(17.5, 24.5) == 42.0
