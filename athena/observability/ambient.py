"""Which run an event belongs to when the emitting code has no run id.

``Recorder.for_run(run_id)`` is right for a research run: the runner knows the
id before it starts and installs the recorder itself. The API process is the
other half of the system and is *not* running a research run — it serves the
chat turns and accepts ``POST /api/runs``. Guardrail decisions made there
(``guardrails/input.py::screen_input``, ``guardrails/tool_output.py::
validate_tool_output``, ``guardrails/output.py::screen_output``) call
:func:`athena.observability.guardrail_events.record_flags`, which resolves the
active recorder — and with none installed the flags were silently dropped.

This module installs one **process-level** recorder for that process and gives
it a run id to write under, in this order:

1. the run id in scope (:func:`run_scope`) — the chat turn's conversation id,
   which the API puts in scope for the duration of the turn
   (``api/app.py::ChatTurnRunId``);
2. otherwise :data:`UNATTRIBUTED_RUN_ID`, and every event written that way
   carries ``extra.run_id_source = "unattributed"`` so a reader can see that the
   record could not be attributed to a run rather than being told a made-up id.

Emission stays passive: nothing here decides anything, changes a flag, or
changes a response. With ``ATHENA_MONITORING=0`` the recorder is disabled and
writes nothing, exactly like every other recorder.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Callable, Iterator

from athena.observability.events import Event
from athena.observability.recorder import Recorder, get_recorder, monitoring_enabled, set_recorder
from athena.observability.store import EventStore, SQLiteEventStore, default_db_path

#: The documented bucket for guardrail rows that could not be attributed to a
#: run. It is a real run id in the event log (readable through
#: ``GET /api/runs/unattributed/telemetry``) precisely so that it is visible.
UNATTRIBUTED_RUN_ID = "unattributed"

_SCOPE: ContextVar[tuple[str, str]] = ContextVar("athena_run_scope", default=("", ""))


def set_run_id(run_id: str | None, *, source: str = "turn") -> Token:
    """Put ``run_id`` in scope for the current task; return the reset token."""
    return _SCOPE.set((str(run_id or "").strip(), str(source or "turn")))


def reset_run_id(token: Token) -> None:
    """Undo :func:`set_run_id`."""
    _SCOPE.reset(token)


def current_run() -> tuple[str, str]:
    """``(run_id, source)`` for the event being written now.

    ``source`` is ``"unattributed"`` when no run id is in scope, which is what
    the event's ``extra.run_id_source`` reports.
    """
    run_id, source = _SCOPE.get()
    if run_id:
        return run_id, source or "turn"
    return UNATTRIBUTED_RUN_ID, "unattributed"


@contextmanager
def run_scope(run_id: str | None, *, source: str = "turn") -> Iterator[str]:
    """Attribute everything recorded inside the block to ``run_id``."""
    token = set_run_id(run_id, source=source)
    try:
        yield str(run_id or "").strip()
    finally:
        reset_run_id(token)


class LazyStore:
    """An ``EventStore`` that opens the SQLite log on first use, per path.

    ``create_app`` runs at import time in some entrypoints and in every test, so
    the event database must not be opened until an event is actually written.
    The path is re-resolved on every emit, so a process whose ``EVENTS_DB``
    changes (every test sets its own) never keeps writing to the previous one.
    """

    def __init__(self, factory: Callable[[], EventStore] = SQLiteEventStore.from_env) -> None:
        self._factory = factory
        self._store: EventStore | None = None
        self._key: object = None

    def _get(self) -> EventStore:
        try:
            key: object = default_db_path()
        except Exception:  # noqa: BLE001 - an unreadable env is not worth a crash
            key = None
        if self._store is None or key != self._key:
            self._store = self._factory()
            self._key = key
        return self._store

    def emit(self, event: Event) -> int:
        return self._get().emit(event)

    def events(self, run_id: str) -> list[Event]:
        return self._get().events(run_id)

    def runs(self) -> list[str]:
        return self._get().runs()


class ContextRecorder(Recorder):
    """A recorder whose run id is resolved from scope at emit time.

    Nothing is mutated on the shared instance: each emission builds the short
    lived ``Recorder`` that writes it, so two concurrent chat turns cannot
    overwrite each other's attribution.
    """

    def __init__(self, store: Any = None) -> None:
        super().__init__(
            UNATTRIBUTED_RUN_ID,
            store if store is not None else LazyStore(),
            monitoring_enabled(),
        )

    def emit(
        self,
        kind: str,
        agent: str | None = None,
        framework: str = "adk",
        **fields: Any,
    ) -> None:
        run_id, source = current_run()
        extra = dict(fields.get("extra") or {})
        extra.setdefault("run_id_source", source)
        fields["extra"] = extra
        target = Recorder(run_id, self.store, self.enabled, self._clock)
        target.emit(kind, agent, framework, **fields)
        self.failures += target.failures


def install_process_recorder(store: Any = None) -> Any:
    """Install the process-level recorder and return the recorder now in effect.

    Deliberately conservative:

    * a recorder that is already installed (a run's own ``Recorder.for_run``, or
      an earlier call to this function) is kept — this never displaces the run
      that is actually executing, and never installs a second writer for the same
      event;
    * otherwise one :class:`ContextRecorder` is installed, so the guardrail
      modules that emit from this process stop writing to nothing.
    """
    existing = get_recorder()
    if existing is not None:
        return existing
    recorder = ContextRecorder(store)
    set_recorder(recorder)
    return recorder


#: Requests whose body names the run their guardrail rows belong to.
#: ``POST /api/chat/messages`` carries ``conversation_id``: a chat turn has no
#: research run id of its own, so the conversation is the run it is recorded
#: under. ``POST /api/runs`` is deliberately absent — that endpoint *creates*
#: the run id, so its flags are attributed explicitly by the handler
#: (``api/app.py::_record_guardrails``) rather than from the request body.
RUN_ID_FIELDS: dict[tuple[str, str], str] = {
    ("POST", "/api/chat/messages"): "conversation_id",
}


def claimed_run(path: str, method: str, body: bytes) -> tuple[str, str]:
    """``(run_id, source)`` a request body names, or ``("", "")`` when it names none."""
    field = RUN_ID_FIELDS.get((str(method).upper(), str(path)), "")
    if not field or not body:
        return "", ""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    value = str(payload.get(field) or "").strip()
    return (value, field) if value else ("", "")


class RunIdScopeMiddleware:
    """Put the request's own run id in scope while the request is handled.

    Pure ASGI, and it only buffers the body of the POST paths listed in
    :data:`RUN_ID_FIELDS`; every other request passes through untouched. The
    buffered bytes are replayed to the application, so nothing downstream
    notices. The scope stays in place until the response has finished, which is
    what makes it visible inside a streaming generator too.

    A request that names no run id (a *new* chat conversation has no
    ``conversation_id`` yet) is left unscoped, and every event written for it
    says ``extra.run_id_source = "unattributed"`` instead of being given a
    made-up id.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        key = (str(scope.get("method") or "").upper(), str(scope.get("path") or ""))
        if scope.get("type") != "http" or key not in RUN_ID_FIELDS:
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            chunks.append(message.get("body") or b"")
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        replayed = False

        async def replay() -> Any:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        run_id, source = claimed_run(key[1], key[0], body)
        if not run_id:
            await self.app(scope, replay, send)
            return
        token = set_run_id(run_id, source=source)
        try:
            await self.app(scope, replay, send)
        finally:
            reset_run_id(token)
