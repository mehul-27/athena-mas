"""The process recorder: guardrail rows survive in the chat/API process.

The chat/API process does not run a research run, so nothing installed a
``Recorder`` in it and every guardrail decision made there resolved to ``None``
and was written to no store at all. ``athena/observability/ambient.py`` fixes
that. What is pinned here:

* ``install_process_recorder`` installs exactly one recorder for the process and
  **never** displaces the recorder a running research run installed;
* attribution resolves at emit time from scope, so two concurrent turns cannot
  overwrite each other;
* a row that names no run is written under ``unattributed`` with
  ``run_id_source="unattributed"`` — visible, never a made-up id;
* ``RunIdScopeMiddleware`` buffers and replays the body of the one POST path it
  maps, and passes every other request through untouched.
"""

from __future__ import annotations

import asyncio

import pytest

from athena.config.reply_guard import note_empty_reply
from athena.observability import ambient
from athena.observability.recorder import Recorder, get_recorder, set_recorder
from athena.observability.store import SQLiteEventStore


@pytest.fixture()
def store(tmp_path) -> SQLiteEventStore:
    return SQLiteEventStore(tmp_path / "events.db")


@pytest.fixture(autouse=True)
def _reset_recorder():
    yield
    set_recorder(None)


# --------------------------------------------------------------- installation
def test_installs_one_process_recorder(store) -> None:
    set_recorder(None)

    installed = ambient.install_process_recorder(store)

    assert installed is not None
    assert get_recorder() is installed
    assert isinstance(installed, ambient.ContextRecorder)


def test_never_displaces_a_running_runs_recorder(store) -> None:
    """A run's own recorder is already installed; installing must keep it."""
    run_recorder = Recorder("run-owner", store)
    set_recorder(run_recorder)

    kept = ambient.install_process_recorder(store)

    assert kept is run_recorder
    assert get_recorder() is run_recorder


def test_installing_twice_does_not_add_a_second_writer(store) -> None:
    set_recorder(None)

    first = ambient.install_process_recorder(store)
    second = ambient.install_process_recorder(store)

    assert first is second


# ---------------------------------------------------------------- attribution
def test_a_row_lands_under_the_run_in_scope(store) -> None:
    set_recorder(ambient.ContextRecorder(store))

    with ambient.run_scope("conv-abc", source="conversation_id"):
        note_empty_reply("adk", 100)

    rows = store.events("conv-abc")
    assert rows
    assert all(row.run_id == "conv-abc" for row in rows)
    assert rows[0].extra["run_id_source"] == "conversation_id"


def test_a_row_with_no_scope_is_visible_not_invented(store) -> None:
    set_recorder(ambient.ContextRecorder(store))

    note_empty_reply("adk", 100)

    rows = store.events(ambient.UNATTRIBUTED_RUN_ID)
    assert rows, "an unattributable row must still be readable"
    assert rows[0].extra["run_id_source"] == "unattributed"


def test_the_scope_is_reset_after_the_block(store) -> None:
    set_recorder(ambient.ContextRecorder(store))

    with ambient.run_scope("conv-one"):
        pass
    note_empty_reply("adk", 100)

    assert store.events("conv-one") == []
    assert store.events(ambient.UNATTRIBUTED_RUN_ID)


def test_two_scopes_do_not_bleed_into_each_other(store) -> None:
    set_recorder(ambient.ContextRecorder(store))

    with ambient.run_scope("conv-a"):
        note_empty_reply("adk", 1)
    with ambient.run_scope("conv-b"):
        note_empty_reply("adk", 2)

    assert len(store.events("conv-a")) == 1
    assert len(store.events("conv-b")) == 1


def test_current_run_reports_the_source(store) -> None:
    with ambient.run_scope("conv-x", source="conversation_id"):
        assert ambient.current_run() == ("conv-x", "conversation_id")

    assert ambient.current_run() == (ambient.UNATTRIBUTED_RUN_ID, "unattributed")


# ------------------------------------------------------------- claimed_run map
def test_claimed_run_reads_the_mapped_field_only() -> None:
    body = b'{"conversation_id": "conv-9", "text": "hi"}'

    assert ambient.claimed_run("/api/chat/messages", "POST", body) == (
        "conv-9", "conversation_id",
    )
    # /api/runs creates the run id, so it is deliberately not mapped.
    assert ambient.claimed_run("/api/runs", "POST", body) == ("", "")
    # A GET on the mapped path is not mapped either.
    assert ambient.claimed_run("/api/chat/messages", "GET", body) == ("", "")


@pytest.mark.parametrize(
    "body", [b"", b"not json", b"[]", b'{"conversation_id": ""}']
)
def test_claimed_run_refuses_unusable_bodies(body: bytes) -> None:
    assert ambient.claimed_run("/api/chat/messages", "POST", body) == ("", "")


# ---------------------------------------------------------------- middleware
def _run_middleware(scope: dict, body: bytes) -> tuple[list, dict]:
    """Drive the middleware once; return (received-body chunks, scope seen)."""
    seen: dict = {}
    chunks: list = []

    async def app(inner_scope, receive, send):
        seen["scope"] = inner_scope
        message = await receive()
        chunks.append(message.get("body") or b"")
        # Prove a second receive() on the replayed stream does not hang.
        more = await receive()
        chunks.append(more.get("body") or b"")

    sent_messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        # A real ASGI receive blocks (or reports a disconnect) once the body is
        # consumed; returning ``http.disconnect`` models that without hanging.
        return sent_messages.pop(0) if sent_messages else {"type": "http.disconnect"}

    async def send(message):
        return None

    asyncio.run(ambient.RunIdScopeMiddleware(app)(scope, receive, send))
    return chunks, seen


def test_middleware_replays_the_body_for_the_mapped_path() -> None:
    body = b'{"conversation_id": "conv-m"}'
    chunks, _ = _run_middleware(
        {"type": "http", "method": "POST", "path": "/api/chat/messages"}, body
    )

    assert chunks[0] == body, "the application must receive the body it sent"


def test_middleware_does_not_buffer_an_unmapped_path() -> None:
    body = b'{"conversation_id": "conv-m"}'
    chunks, _ = _run_middleware(
        {"type": "http", "method": "POST", "path": "/api/runs"}, body
    )

    assert chunks[0] == body


def test_middleware_puts_the_conversation_id_in_scope() -> None:
    """The scope must still be resolvable while the (streaming) request runs."""
    observed: list = []
    body = b'{"conversation_id": "conv-scope"}'

    async def app(inner_scope, receive, send):
        await receive()
        observed.append(ambient.current_run())

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        return None

    asyncio.run(
        ambient.RunIdScopeMiddleware(app)(
            {"type": "http", "method": "POST", "path": "/api/chat/messages"},
            receive,
            send,
        )
    )

    assert observed == [("conv-scope", "conversation_id")]


def test_middleware_leaves_a_non_http_scope_alone() -> None:
    called: list = []

    async def app(inner_scope, receive, send):
        called.append(inner_scope)

    async def receive():
        raise AssertionError("must not read a websocket/lifespan scope")

    async def send(message):
        return None

    asyncio.run(
        ambient.RunIdScopeMiddleware(app)({"type": "lifespan"}, receive, send)
    )

    assert called == [{"type": "lifespan"}]
