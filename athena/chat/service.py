"""One interactive chat turn: history, grounding, a bounded tool loop, finalisation."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable

from athena.chat import grounding, prompts, tools
from athena.chat.llm import stream_completion
from athena.chat.models import (
    CapabilityError,
    ChatConfigError,
    ChatTarget,
    Grounded,
)
from athena.chat.store import DEFAULT_TITLE, ChatStore, derive_title
from athena.observability.redact import redact

MAX_ROUNDS = int(os.getenv("ATHENA_CHAT_MAX_ROUNDS") or 4)
MAX_TOOL_CALLS = int(os.getenv("ATHENA_CHAT_MAX_TOOL_CALLS") or 8)
TOOL_TIMEOUT = float(os.getenv("ATHENA_CHAT_TOOL_TIMEOUT_S") or 30)
HISTORY_TURNS = int(os.getenv("ATHENA_CHAT_HISTORY_TURNS") or 20)
HISTORY_CHARS = int(os.getenv("ATHENA_CHAT_HISTORY_CHARS") or 12000)
MAX_TOKENS = int(os.getenv("ATHENA_CHAT_MAX_TOKENS") or 3000)

KNOWN_CAPABILITIES = ("rag", "web_search")
NO_TOOL_SUPPORT = (
    "This model does not support tool calling, so it answered without using the selected tools."
)


@dataclass
class TurnContext:
    store: ChatStore
    conversation_id: str
    conversation: dict[str, Any]
    history: list[dict[str, str]]
    user_message: dict[str, Any]
    assistant_message: dict[str, Any]
    target: ChatTarget
    capabilities: tuple[str, ...]
    selection: tools.ToolSelection
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)


def normalize_capabilities(values: Iterable[Any] | None) -> tuple[str, ...]:
    cleaned: list[str] = []
    for raw in values or []:
        cap = str(raw or "").strip().lower()
        if cap and cap not in cleaned:
            cleaned.append(cap)
    unknown = [cap for cap in cleaned if cap not in KNOWN_CAPABILITIES]
    if unknown:
        raise CapabilityError(f"Unknown capability: {', '.join(unknown)}")
    return tuple(cap for cap in KNOWN_CAPABILITIES if cap in cleaned)


def _check_available(capabilities: tuple[str, ...], target: ChatTarget) -> None:
    if "rag" in capabilities:
        usable, reason = tools.availability("rag_search", target)
        if not usable:
            raise ChatConfigError(reason, status=409)
    if "web_search" in capabilities:
        usable, reason = tools.availability("web_search", target)
        if not usable:
            raise ChatConfigError(reason, status=409)


def prepare_turn(
    store: ChatStore,
    *,
    conversation_id: str | None,
    message: str,
    profile_id: str | None,
    capabilities: Iterable[Any] | None,
    tool_names: Iterable[Any] | None,
) -> TurnContext:
    """Validate and persist the turn before any SSE headers are sent."""
    from athena.chat.target import resolve_target

    if conversation_id:
        conversation = store.get(conversation_id)
        if conversation is None:
            raise ChatConfigError(f"No conversation {conversation_id!r}.", status=404)
    else:
        conversation = store.create()

    history = store.history(conversation["id"], HISTORY_TURNS, HISTORY_CHARS)
    target = resolve_target(profile_id, conversation.get("profile_id") or "")
    caps = normalize_capabilities(capabilities)
    _check_available(caps, target)
    selection = tools.resolve_selection(tool_names, target)

    user = store.add_message(
        conversation["id"],
        "user",
        message,
        capabilities=caps,
        tools=selection.allowed,
        profile_id=target.profile_id,
        meta={
            "rejected_tools": list(selection.rejected),
            "rejected_reasons": selection.reason_map(),
        },
    )
    assistant = store.add_message(
        conversation["id"],
        "assistant",
        "",
        status="streaming",
        provider=target.provider,
        model=target.model,
        profile_id=target.profile_id,
        capabilities=caps,
        tools=selection.allowed,
        meta={
            "rejected_tools": list(selection.rejected),
            "rejected_reasons": selection.reason_map(),
        },
    )
    if profile_id and profile_id != (conversation.get("profile_id") or ""):
        store.set_profile(conversation["id"], target.profile_id)
    if not history and (conversation.get("title") or "") == DEFAULT_TITLE:
        store.rename(conversation["id"], derive_title(message))

    summary = store.summary_by_id(conversation["id"]) or store.summary(conversation)
    return TurnContext(
        store=store,
        conversation_id=conversation["id"],
        conversation=summary,
        history=history,
        user_message=user,
        assistant_message=assistant,
        target=target,
        capabilities=caps,
        selection=selection,
    )


def _decode_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _finalize(
    ctx: TurnContext,
    *,
    content: str,
    status: str,
    usage: dict[str, Any] | None,
    tool_events: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    error: str = "",
    tool_note: str = "",
    notes: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = {
        "tool_note": tool_note,
        "grounding_notes": list(notes or []),
        "rejected_tools": list(ctx.selection.rejected),
        "rejected_reasons": ctx.selection.reason_map(),
    }
    message = ctx.store.update_message(
        ctx.assistant_message["id"],
        content=content,
        status=status,
        provider=ctx.target.provider,
        model=ctx.target.model,
        profile_id=ctx.target.profile_id,
        sources=sources,
        tool_events=tool_events,
        usage=usage,
        error=error,
        meta=meta,
    )
    conversation = ctx.store.summary_by_id(ctx.conversation_id) or ctx.conversation
    return message, conversation


async def stream_turn(ctx: TurnContext) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield `(event_name, payload)` tuples for one turn.

    The route serialises these as SSE. Validation and persistence already
    happened in `prepare_turn`, so nothing here may raise HTTP errors.
    """
    yield (
        "meta",
        {
            "conversation": ctx.conversation,
            "user_message": ctx.user_message,
            "assistant_message": ctx.assistant_message,
            "target": ctx.target.public(),
        },
    )

    notes: list[str] = []
    if ctx.selection.rejected:
        reasons = ctx.selection.reason_map()
        detail = "; ".join(
            f"{name}: {reasons.get(name, 'not available')}" for name in ctx.selection.rejected
        )
        notes.append(f"Some selected tools were not used ({detail}).")
        yield ("note", {"text": notes[-1]})

    grounded: Grounded | None = None
    if ctx.capabilities:
        grounded = await grounding.collect(
            ctx.user_message["content"], ctx.capabilities, ctx.target
        )
        if grounded.sources:
            yield ("sources", {"sources": grounded.sources})
        for note in grounded.notes:
            notes.append(note)
            yield ("note", {"text": note})
        if grounded.refusal:
            message, conversation = _finalize(
                ctx,
                content=grounded.refusal,
                status="ok",
                usage=None,
                tool_events=[],
                sources=[],
                notes=notes,
            )
            yield ("delta", {"text": grounded.refusal})
            yield ("done", {"message": message, "conversation": conversation})
            return

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompts.system_message(ctx.target, bool(ctx.capabilities))}
    ]
    if grounded and grounded.context:
        messages.append({"role": "system", "content": prompts.context_block(grounded)})
    messages.extend(ctx.history)
    messages.append({"role": "user", "content": ctx.user_message["content"]})

    schemas = tools.schemas(ctx.selection.allowed)
    tool_events: list[dict[str, Any]] = []
    sources = grounded.sources if grounded else []
    all_text = ""
    tool_note = ""
    calls_used = 0
    model_calls = 0
    usage_in = 0
    usage_out = 0
    reasoning_tokens = 0
    usage_reported = False
    truncated = False

    try:
        for round_index in range(1, MAX_ROUNDS + 1):
            if ctx.stop_event.is_set():
                break
            round_text = ""
            calls: dict[int, dict[str, str]] = {}
            try:
                model_calls += 1
                async for delta in stream_completion(
                    ctx.target,
                    messages,
                    tools=schemas if schemas else None,
                    max_tokens=MAX_TOKENS,
                ):
                    if ctx.stop_event.is_set():
                        break
                    if delta.usage:
                        usage_reported = True
                        usage_in += int(delta.usage.get("tokens_in") or 0)
                        usage_out += int(delta.usage.get("tokens_out") or 0)
                        reasoning_tokens += int(
                            delta.usage.get("reasoning_tokens") or 0
                        )
                    if delta.text:
                        round_text += delta.text
                        all_text += delta.text
                        yield ("delta", {"text": delta.text})
                    if delta.tool_index >= 0:
                        call = calls.setdefault(
                            delta.tool_index, {"id": "", "name": "", "arguments": ""}
                        )
                        if delta.tool_id:
                            call["id"] = delta.tool_id
                        if delta.tool_name:
                            call["name"] = delta.tool_name
                        if delta.tool_arguments:
                            call["arguments"] += delta.tool_arguments
            except Exception:
                if round_index == 1 and schemas and not round_text and not calls:
                    # The endpoint may reject the `tools` field outright. Answer
                    # without tools rather than failing the message.
                    schemas = []
                    tool_note = NO_TOOL_SUPPORT
                    yield ("note", {"text": tool_note})
                    model_calls += 1
                    async for delta in stream_completion(
                        ctx.target, messages, tools=None, max_tokens=MAX_TOKENS
                    ):
                        if ctx.stop_event.is_set():
                            break
                        if delta.usage:
                            usage_reported = True
                            usage_in += int(delta.usage.get("tokens_in") or 0)
                            usage_out += int(delta.usage.get("tokens_out") or 0)
                            reasoning_tokens += int(
                                delta.usage.get("reasoning_tokens") or 0
                            )
                        if delta.text:
                            round_text += delta.text
                            all_text += delta.text
                            yield ("delta", {"text": delta.text})
                else:
                    raise
            if ctx.stop_event.is_set():
                break
            if not calls:
                break

            tool_calls = []
            for index in sorted(calls):
                call = calls[index]
                call_id = call["id"] or f"call_{round_index}_{index}"
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"] or "{}",
                        },
                    }
                )
            messages.append(
                {"role": "assistant", "content": round_text or "", "tool_calls": tool_calls}
            )

            for call in tool_calls:
                if ctx.stop_event.is_set() or calls_used >= MAX_TOOL_CALLS:
                    truncated = True
                    break
                name = call["function"]["name"]
                call_id = call["id"]
                args = _decode_args(call["function"]["arguments"])
                if name not in ctx.selection.allowed or name not in tools.TOOL_SPECS:
                    result_text = f"{name or 'That tool'} is not enabled for this message."
                    event = {
                        "name": name or "(unknown)",
                        "label": name or "Unknown tool",
                        "status": "error",
                        "summary": "Not enabled",
                        "elapsed_ms": 0.0,
                        "error": result_text,
                    }
                else:
                    spec = tools.TOOL_SPECS[name]
                    yield (
                        "tool",
                        {
                            "id": call_id,
                            "name": name,
                            "label": spec.label,
                            "status": "running",
                            "round": round_index,
                            "summary": "",
                            "elapsed_ms": 0.0,
                            "error": "",
                        },
                    )
                    result_text, event = await tools.execute(
                        name, args, ctx.target, timeout=TOOL_TIMEOUT
                    )
                    calls_used += 1
                event["id"] = call_id
                event["round"] = round_index
                tool_events.append(event)
                yield ("tool", event)
                messages.append(
                    {"role": "tool", "tool_call_id": call_id, "content": result_text}
                )
            if truncated:
                break
        else:
            truncated = True
            tool_note = tool_note or f"Stopped after {MAX_ROUNDS} tool rounds."
            yield ("note", {"text": tool_note})

        if truncated and not ctx.stop_event.is_set():
            if not tool_note:
                tool_note = f"Stopped after {MAX_ROUNDS} tool rounds."
                yield ("note", {"text": tool_note})
            model_calls += 1
            async for delta in stream_completion(
                ctx.target, messages, tools=None, max_tokens=MAX_TOKENS
            ):
                if ctx.stop_event.is_set():
                    break
                if delta.usage:
                    usage_reported = True
                    usage_in = max(usage_in, int(delta.usage.get("tokens_in") or 0))
                    usage_out = max(usage_out, int(delta.usage.get("tokens_out") or 0))
                    reasoning_tokens = max(
                        reasoning_tokens, int(delta.usage.get("reasoning_tokens") or 0)
                    )
                if delta.text:
                    all_text += delta.text
                    yield ("delta", {"text": delta.text})

        content = all_text.strip()
        if ctx.stop_event.is_set():
            status = "stopped"
            error = ""
        elif not content:
            status = "error"
            error = "The model returned no text. Try a different model or profile."
        else:
            status = "ok"
            error = ""

        usage = None
        if usage_reported:
            usage = {
                "provider": ctx.target.provider,
                "model": ctx.target.model,
                "tokens_in": usage_in,
                "tokens_out": usage_out,
                "reasoning_tokens": reasoning_tokens,
                "calls": model_calls,
            }
            yield ("usage", usage)

        message, conversation = _finalize(
            ctx,
            content=content,
            status=status,
            usage=usage,
            tool_events=tool_events,
            sources=sources,
            error=error,
            tool_note=tool_note,
            notes=notes,
        )
        yield ("done", {"message": message, "conversation": conversation})

    except asyncio.CancelledError:
        _finalize(
            ctx,
            content=all_text.strip(),
            status="stopped",
            usage=None,
            tool_events=tool_events,
            sources=sources,
            tool_note=tool_note,
            notes=notes,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - after headers this must be an event
        detail = (redact(f"{type(exc).__name__}: {exc}") or "provider error")[:300]
        message, conversation = _finalize(
            ctx,
            content=all_text.strip(),
            status="error",
            usage=None,
            tool_events=tool_events,
            sources=sources,
            error=detail,
            tool_note=tool_note,
            notes=notes,
        )
        yield ("error", {"detail": detail, "code": "chat_failed"})
        yield ("done", {"message": message, "conversation": conversation})
