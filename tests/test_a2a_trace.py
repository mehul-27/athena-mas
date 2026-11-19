"""Trace linkage across the ADK client and CrewAI's real A2A routes."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry.context import attach, detach
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from athena.agents import a2a_client, crew_server
from athena.agents.a2a_stage import DEFAULT_CARD_URL, build_a2a_stage
from athena.blackboard import Blackboard, SQLiteStore, Task
from athena.observability import a2a_trace
from athena.observability.recorder import Recorder, get_recorder, recorder_scope
from athena.observability.store import SQLiteEventStore
from athena.patterns.context import P2PContext
from athena.patterns.p2p import build as build_p2p


@pytest.fixture()
def a2a(monkeypatch, tmp_path):
    # The rest of the offline suite may disable the global SDK at startup.
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    provider = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False)
    monkeypatch.setattr(a2a_trace, "_TRACER", provider.get_tracer(a2a_trace.__name__))
    monkeypatch.setenv("ATHENA_MONITORING", "1")
    monkeypatch.setenv("ATHENA_A2A_SHARED_TOKEN", "test-token")
    monkeypatch.setenv("ATHENA_A2A_REQUIRE_SIG", "1")
    monkeypatch.setattr(crew_server, "configure_process", lambda: None)
    monkeypatch.setattr(crew_server, "install_usage_logger", lambda: None)
    monkeypatch.setattr(crew_server, "callback_count", lambda: 0)

    board_store = SQLiteStore(tmp_path / "blackboard.db")
    board = Blackboard.create(Task(question="What is systems engineering?"), store=board_store)
    events = SQLiteEventStore.from_env()

    def run_analyst(run_id, instruction, store=None, context=None):
        get_recorder().emit("tool_call", "analyst", "crewai")
        if context is not None:
            return {"content": json.dumps({"key_findings": ["One"], "gaps": []})}
        return {"section": "analysis", "version": 1}

    app = crew_server.create_app({"analyst": run_analyst}, board_store)
    requests = []

    async def capture(request):
        requests.append(request)

    def make_http(**kwargs):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), event_hooks={"request": [capture]}, **kwargs
        )

    monkeypatch.setattr(a2a_client, "httpx", SimpleNamespace(AsyncClient=make_http))
    return board, events, requests


@pytest.mark.parametrize("mode", ["blackboard", "p2p"])
def test_traceparent_links_adk_and_remote_crewai_events(a2a, mode):
    board, events, requests = a2a
    upstream_trace = "abcd" * 8
    upstream_span = "1234" * 4

    async def run():
        if mode == "p2p":
            agent = build_p2p(board.run_id, board.store)
            assert json.loads(await agent._call_role("analyst", P2PContext("question")))["gaps"] == []
        else:
            agent = build_a2a_stage("analyst", board.run_id)
            result = [event async for event in agent._run_async_impl(None)]
            assert json.loads(result[0].content.parts[0].text)["ok"] is True

    parent = TraceContextTextMapPropagator().extract({
        "traceparent": f"00-{upstream_trace}-{upstream_span}-01",
        "tracestate": "athena=test",
    })
    token = attach(parent)
    try:
        with recorder_scope(Recorder(board.run_id, events)):
            asyncio.run(run())
    finally:
        detach(token)

    rows = events.events(board.run_id)
    (client,) = (row for row in rows if row.kind == "a2a_call" and row.framework == "adk")
    server = [row for row in rows if row.framework == "crewai"]
    assert [row.kind for row in server] == ["stage_start", "tool_call", "stage_end"]
    assert client.extra["span_kind"] == SpanKind.CLIENT.name.lower()
    assert {row.extra["span_kind"] for row in server} == {SpanKind.SERVER.name.lower()}
    assert {client.extra["trace_id"], *(row.extra["trace_id"] for row in server)} == {upstream_trace}
    assert client.extra["parent_span_id"] == upstream_span
    assert {row.extra["parent_span_id"] for row in server} == {client.extra["span_id"]}
    assert len({row.extra["span_id"] for row in server}) == 1
    assert server[0].extra["span_id"] != client.extra["span_id"]

    discovery, message = requests
    assert "traceparent" not in discovery.headers
    assert message.headers["traceparent"] == f"00-{upstream_trace}-{client.extra['span_id']}-01"
    assert message.headers["tracestate"] == "athena=test"


def test_remote_request_with_invalid_traceparent_starts_a_local_span(a2a, monkeypatch):
    board, events, requests = a2a
    make_http = a2a_client.httpx.AsyncClient
    monkeypatch.setattr(a2a_client.httpx, "AsyncClient", lambda **kw: make_http(
        headers={"traceparent": "00-invalid-parent", "baggage": "other=external"}, **kw
    ))
    ack = asyncio.run(a2a_client.send_role_request(
        DEFAULT_CARD_URL, a2a_client.build_payload(board.run_id, "analyst", "go"),
    ))
    assert json.loads(ack)["ok"] is True
    assert requests[-1].headers["traceparent"] == "00-invalid-parent"
    server = events.events(board.run_id)[0].extra
    assert int(server["trace_id"], 16) != 0
    assert len(server["trace_id"]) == 32
    assert "parent_span_id" not in server
    assert "baggage" not in server


def test_p2p_transport_failure_persists_client_span(a2a, monkeypatch):
    board, events, _ = a2a

    async def fail(request):
        raise httpx.ConnectError("CrewAI offline")

    monkeypatch.setattr(a2a_client.httpx, "AsyncClient", lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(fail), **kwargs
    ))
    agent = build_p2p(board.run_id, board.store)
    with recorder_scope(Recorder(board.run_id, events)), pytest.raises(Exception, match="CrewAI offline"):
        asyncio.run(agent._call_role("analyst", P2PContext("question")))

    (error,) = events.events(board.run_id)
    assert error.framework == "adk"
    assert error.kind == "error"
    assert error.extra["span_kind"] == "client"
    assert len(error.extra["trace_id"]) == 32
    assert len(error.extra["span_id"]) == 16
