"""The p2p critic must actually receive the writer's report.

Two levels:

* ``test_*_through_the_real_supervisor`` needs a local A2A server, so it is
  skipped unless ``ATHENA_REAL_STACK=1`` (the test sandbox here blocks port
  binding: uvicorn never starts and the client gets "connection refused").
* ``test_*_with_the_transport_stubbed`` keeps everything above the A2A client
  real - the supervisor's ``_call_role``, the context object, the 6k builder -
  and replaces only ``send_role_request``, which is one layer below the code
  under test. This is what proves the bug: the stage output now lands in the
  context that the critic's message is built from.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from athena.blackboard import Blackboard, SQLiteStore, Task
from athena.patterns import p2p as p2p_module
from athena.patterns.context import P2PContext, context_budget

ANALYSIS = "ANALYST-FINDING: systems engineering is interdisciplinary."
ANSWER = "WRITER-ANSWER: the handbook calls it an interdisciplinary discipline."
POINT = "WRITER-KEYPOINT: it covers the whole life cycle."
REPORT = json.dumps({"answer": ANSWER, "key_points": [POINT], "citations": [],
                     "confidence": 0.8, "tools_used": [], "run_id": "r"})
CRITIQUE = json.dumps({"issues": [], "severity": "low", "approved": True})


@pytest.fixture()
def blackboard(tmp_path) -> Blackboard:
    return Blackboard.create(Task(question="What is systems engineering?"),
                             store=SQLiteStore(tmp_path / "bb.db"))


def _stub_transport(monkeypatch) -> list[dict]:
    """Replace only the A2A client; record what each role was sent."""
    sent: list[dict] = []

    async def fake_send(card_url: str, payload: str, timeout=None) -> str:
        body = json.loads(payload)
        sent.append(body)
        content = {"analyst": ANALYSIS, "writer": REPORT, "critic": CRITIQUE}[body["role"]]
        return json.dumps({"ok": True, "role": body["role"],
                           "section": body["role"], "content": content})

    monkeypatch.setattr(p2p_module, "send_role_request", fake_send)
    return sent


def _run(blackboard: Blackboard, roles=("writer", "critic")) -> P2PContext:
    agent = p2p_module.build(blackboard.run_id, blackboard.store, card_url="http://stub")
    context = P2PContext(question="What is systems engineering?")
    context.analysis = ANALYSIS

    async def drive() -> None:
        for role in roles:
            await agent._call_role(role, context)

    asyncio.run(drive())
    return context


def test_the_critic_gets_the_writers_report(blackboard, monkeypatch) -> None:
    sent = _stub_transport(monkeypatch)

    _run(blackboard, roles=("analyst", "writer", "critic"))

    critic = [item for item in sent if item["role"] == "critic"][-1]
    assert ANSWER in critic["context"], "the critic did not receive the answer"
    assert POINT in critic["context"], "the critic did not receive the key points"
    assert ANALYSIS in critic["context"]
    assert len(critic["context"]) <= context_budget()


def test_the_writer_gets_the_analysis(blackboard, monkeypatch) -> None:
    sent = _stub_transport(monkeypatch)

    _run(blackboard, roles=("analyst", "writer"))

    writer = [item for item in sent if item["role"] == "writer"][-1]
    assert ANALYSIS in writer["context"]
    assert ANSWER not in writer["context"], "the writer runs before the report exists"
    assert len(writer["context"]) <= context_budget()


def test_the_context_stays_within_the_cap(blackboard, monkeypatch) -> None:
    sent = _stub_transport(monkeypatch)

    _run(blackboard, roles=("analyst", "writer", "critic"))

    for item in sent:
        assert len(item["context"]) <= context_budget()
        assert len(json.dumps(item)) < 20000


def test_blackboard_mode_ships_only_the_instruction(tmp_path) -> None:
    """The contrast: blackboard mode carries no report at all."""
    from athena.agents.crew_server import run_role_request
    from athena.blackboard import SQLiteStore as Store

    store = Store(tmp_path / "bb2.db")
    board = Blackboard.create(Task(question="q"), store=store)
    big = json.dumps({"run_id": board.run_id, "role": "critic",
                      "instruction": "review"})
    seen: list[dict] = []

    def stub(run_id, instruction, store=None):
        seen.append({"run_id": run_id, "instruction": instruction})
        return {"section": "critique", "content": CRITIQUE,
                "tokens_in": 1, "tokens_out": 1}

    ack = json.loads(run_role_request(big, roles={"critic": stub}, store=store))

    assert ack["ok"] is True
    assert len(json.dumps(seen[0])) < 600, "blackboard mode ships no context"
    assert ANSWER not in json.dumps(seen[0])


@pytest.mark.skipif(
    os.getenv("ATHENA_REAL_STACK") != "1",
    reason="needs a local A2A server; the test sandbox blocks port binding",
)
def test_the_critic_gets_the_report_through_the_real_supervisor(tmp_path, monkeypatch) -> None:
    """Full stack: real uvicorn crew server, real A2A client, fake provider."""
    import socket
    import sys
    import threading
    import time
    from pathlib import Path

    import uvicorn

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fake_provider import FakeProvider

    provider = FakeProvider(answer=ANSWER, key_points=[POINT]).start()
    monkeypatch.setenv("LLM_API_BASE", provider.base_url)
    monkeypatch.setenv("LLM_API_KEY", "dummy-key-for-fake-provider")
    monkeypatch.setenv("LLM_MODEL", "openai/fake-model")
    from athena.agents.crew_server import create_app

    create_app()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 20
    while time.time() < deadline and not server.started:
        time.sleep(0.1)

    try:
        store = SQLiteStore(tmp_path / "bb3.db")
        board = Blackboard.create(Task(question="q"), store=store)
        agent = p2p_module.build(board.run_id, store, card_url=f"http://127.0.0.1:{port}")
        context = P2PContext(question="What is systems engineering?")
        context.analysis = ANALYSIS

        async def drive() -> None:
            await agent._call_role("writer", context)
            await agent._call_role("critic", context)

        asyncio.run(drive())

        critic_bodies = [
            json.dumps(request["body"]) for request in provider.requests
            if ANSWER in json.dumps(request["body"]) and "critic" in json.dumps(request["body"]).lower()
        ]
        assert critic_bodies, "the critic's request did not carry the report"
        assert POINT in critic_bodies[-1]
    finally:
        server.should_exit = True
        provider.stop()
