"""The interactive chat API, separate from the research run API."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from athena.api.guards import guard
from athena.chat import service, tools
from athena.chat.models import (
    CapabilityError,
    ChatConfigError,
    ChatMessageIn,
    CreateConversationIn,
    RenameConversationIn,
    SetModelIn,
)
from athena.chat.store import ChatStore
from athena.chat.target import models_view, search_settings
from athena.observability.redact import redact
from athena.settings import profiles as profile_store
from athena.tools.rag.status import chunk_count

router = APIRouter(prefix="/api/chat", tags=["chat"])

# One active stream per conversation. Chat allows concurrent conversations,
# unlike the single research run.
_RUNS: dict[str, dict[str, Any]] = {}


def _store() -> ChatStore:
    return ChatStore()


def _require(store: ChatStore, conversation_id: str) -> dict:
    conversation = store.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail=f"no conversation {conversation_id!r}")
    return conversation


def _summary(store: ChatStore, conversation_id: str) -> dict:
    summary = store.summary_by_id(conversation_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"no conversation {conversation_id!r}")
    return summary


def _search_target(profile_id: str | None):
    provider, endpoint = search_settings(profile_id)
    return SimpleNamespace(search_provider=provider, search_endpoint=endpoint)


@router.get("/models")
def models(request: Request) -> dict:
    guard(request)
    return models_view()


@router.get("/capabilities")
def capabilities(request: Request, profile_id: str | None = None) -> dict:
    guard(request)
    provider, _endpoint = search_settings(profile_id)
    rag_ok = chunk_count() > 0
    web_ok = provider in {"duckduckgo", "searxng"}
    return {
        "capabilities": [
            {
                "id": "rag",
                "label": "Documents",
                "description": "Ground the answer in the indexed documents.",
                "available": rag_ok,
                "reason": ""
                if rag_ok
                else "No documents are indexed. Rebuild the index in Settings → Library.",
            },
            {
                "id": "web_search",
                "label": "Web search",
                "description": "Ground the answer in live web results.",
                "available": web_ok,
                "reason": ""
                if web_ok
                else (
                    "Web search is disabled."
                    if provider == "disabled"
                    else f"Unsupported search provider {provider!r}."
                ),
            },
        ]
    }


@router.get("/tools")
def tool_catalog(request: Request, profile_id: str | None = None) -> dict:
    guard(request)
    return {"tools": tools.catalog(_search_target(profile_id))}


@router.get("/conversations")
def list_conversations(request: Request, limit: int = 50) -> dict:
    guard(request)
    return {"conversations": _store().list(limit)}


@router.post("/conversations")
def create_conversation(payload: CreateConversationIn, request: Request) -> dict:
    guard(request)
    store = _store()
    conversation = store.create(payload.title or "New chat")
    return {"conversation": store.summary(conversation)}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, request: Request) -> dict:
    guard(request)
    store = _store()
    _require(store, conversation_id)
    return {
        "conversation": _summary(store, conversation_id),
        "messages": store.messages(conversation_id),
    }


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str, payload: RenameConversationIn, request: Request
) -> dict:
    guard(request)
    store = _store()
    _require(store, conversation_id)
    if not store.rename(conversation_id, payload.title):
        raise HTTPException(status_code=422, detail="title cannot be empty")
    return {"conversation": _summary(store, conversation_id)}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, request: Request) -> dict:
    guard(request)
    store = _store()
    _require(store, conversation_id)
    result = store.delete(conversation_id)
    return result


@router.put("/conversations/{conversation_id}/model")
def set_conversation_model(
    conversation_id: str, payload: SetModelIn, request: Request
) -> dict:
    guard(request)
    store = _store()
    _require(store, conversation_id)
    profile_id = (payload.profile_id or "").strip()
    if profile_id and profile_store.find(profile_id) is None:
        raise HTTPException(status_code=404, detail=f"no profile {profile_id!r}")
    store.set_profile(conversation_id, profile_id)
    return {"conversation": _summary(store, conversation_id)}


def _transcript(conversation: dict, messages: list[dict]) -> str:
    lines = [f"# {conversation.get('title') or 'Chat'}", ""]
    for message in messages:
        if message["role"] == "user":
            lines += ["**User**", message.get("content") or "", ""]
        elif message["role"] == "assistant":
            attribution = ""
            if message.get("provider") or message.get("model"):
                attribution = (
                    f" _(via {message.get('provider') or '?'} · "
                    f"{message.get('model') or '?'})_"
                )
            lines += [f"**Athena**{attribution}", message.get("content") or ""]
            for source in message.get("sources") or []:
                label = source.get("title") or source.get("document") or source.get("url")
                if label:
                    lines.append(f"- {label}")
            if message.get("error"):
                lines.append(f"[error] {message['error']}")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


@router.get("/conversations/{conversation_id}/transcript", response_class=PlainTextResponse)
def transcript(conversation_id: str, request: Request) -> PlainTextResponse:
    guard(request)
    store = _store()
    conversation = _require(store, conversation_id)
    text = _transcript(conversation, store.messages(conversation_id))
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")


@router.post("/conversations/{conversation_id}/stop")
def stop(conversation_id: str, request: Request) -> dict:
    guard(request)
    run = _RUNS.get(conversation_id)
    if run is None:
        return {"stopped": False}
    run["event"].set()
    task = run.get("task")
    if task is not None and not task.done():
        task.cancel()
    return {"stopped": True}


@router.post("/messages")
def send_message(payload: ChatMessageIn, request: Request) -> StreamingResponse:
    guard(request)
    store = _store()
    if payload.conversation_id and payload.conversation_id in _RUNS:
        raise HTTPException(
            status_code=409, detail="a reply is already streaming in this conversation"
        )
    try:
        ctx = service.prepare_turn(
            store,
            conversation_id=payload.conversation_id,
            message=payload.message,
            profile_id=(payload.profile_id or "").strip() or None,
            capabilities=payload.capabilities,
            tool_names=payload.tools,
        )
    except ChatConfigError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except CapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a bad turn is a 422, not a 500
        raise HTTPException(
            status_code=422, detail=f"could not start chat: {type(exc).__name__}"
        ) from exc
    return StreamingResponse(
        _frames(ctx, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _frames(ctx: service.TurnContext, request: Request):
    queue: asyncio.Queue = asyncio.Queue()

    async def produce() -> None:
        try:
            async for event, payload in service.stream_turn(ctx):
                await queue.put((event, payload))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - after headers, report as an event
            detail = (redact(f"{type(exc).__name__}: {exc}") or "chat failed")[:300]
            await queue.put(("error", {"detail": detail, "code": "chat_failed"}))
            try:
                message = ctx.store.update_message(
                    ctx.assistant_message["id"], status="error", error=detail
                )
                conversation = (
                    ctx.store.summary_by_id(ctx.conversation_id) or ctx.conversation
                )
                await queue.put(("done", {"message": message, "conversation": conversation}))
            except Exception:  # noqa: BLE001 - the error frame is already sent
                pass
        finally:
            await queue.put(("__end__", {}))

    task = asyncio.create_task(produce())
    _RUNS[ctx.conversation_id] = {"event": ctx.stop_event, "task": task}
    seq = 0
    try:
        while True:
            if await request.is_disconnected():
                ctx.stop_event.set()
                task.cancel()
                break
            try:
                event, payload = await asyncio.wait_for(queue.get(), timeout=10)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if event == "__end__":
                break
            seq += 1
            yield (
                f"id: {seq}\nevent: {event}\n"
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            )
            if event == "done":
                break
    finally:
        _RUNS.pop(ctx.conversation_id, None)
        if not task.done():
            ctx.stop_event.set()
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
