"""One interactive chat turn: history, grounding, a bounded tool loop, finalisation.

The guardrails layer runs at three points in this path and nowhere is it allowed
to crash a turn:

* the user's message is screened before anything is stored; a *blocking* verdict
  refuses the turn, everything else is only flagged, and the model is given the
  legitimate remainder of the message rather than the injected directives;
* every grounded source is wrapped in the untrusted-content boundary;
* every tool result is validated before it is passed back to the model;
* the finished answer is screened for secrets, toxicity and obeyed injection.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable

from athena.blackboard.schemas import GuardrailFlag
from athena.chat import grounding, prompts, tools
from athena.chat.llm import stream_completion
from athena.chat.models import (
    CapabilityError,
    ChatConfigError,
    ChatTarget,
    Grounded,
)
from athena.chat.store import DEFAULT_TITLE, ChatStore, derive_title
from athena.guardrails.injection import scan_injection
from athena.guardrails.input import refusal_message as guardrail_refusal
from athena.guardrails.input import screen_input
from athena.guardrails.output import screen_output
from athena.guardrails.tool_output import validate_tool_output
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
    # The user's message with injected directives removed: this, not the stored
    # text, is what the model and the retrieval query see.
    model_text: str = ""
    guardrail_flags: list[GuardrailFlag] = field(default_factory=list)
    guardrail_categories: list[str] = field(default_factory=list)
    injection_hits: list[Any] = field(default_factory=list)


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

    # Screen the message before anything is created, persisted or sent anywhere.
    # Only the short harm list blocks; injection and abuse are flagged and
    # neutralised, so a rude or manipulative message that still asks a real
    # question is answered.
    verdict = screen_input(message)
    if verdict.blocking:
        raise ChatConfigError(guardrail_refusal(verdict), status=422)
    model_text = verdict.model_text or message

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
            "guardrail_categories": list(verdict.categories),
            "guardrail_flags": [flag.model_dump() for flag in verdict.flags],
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
            "guardrail_categories": list(verdict.categories),
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
        model_text=model_text,
        guardrail_flags=list(verdict.flags),
        guardrail_categories=list(verdict.categories),
        injection_hits=list(verdict.hits),
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


def _trace_upsert(trace: list[dict[str, Any]], event: dict[str, Any]) -> dict[str, Any]:
    """Replace an event with the same id, preserving first-seen order."""
    for index, item in enumerate(trace):
        if item.get("id") == event.get("id"):
            trace[index] = event
            return event
    trace.append(event)
    return event


def _guardrail_rows(flags: Iterable[GuardrailFlag]) -> list[dict[str, Any]]:
    """Deduplicated flag rows for the stored message metadata."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for flag in flags:
        key = (flag.stage, flag.rule, flag.detail)
        if key in seen:
            continue
        seen.add(key)
        rows.append(flag.model_dump())
    return rows


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
    trace: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    output_flags = screen_output(content, injection_hits=ctx.injection_hits)
    flags = _guardrail_rows([*ctx.guardrail_flags, *output_flags])
    meta = {
        "tool_note": tool_note,
        "grounding_notes": list(notes or []),
        "rejected_tools": list(ctx.selection.rejected),
        "rejected_reasons": ctx.selection.reason_map(),
        "trace": list(trace or []),
        "guardrail_flags": flags,
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
    trace: list[dict[str, Any]] = []
    started_at = time.perf_counter()

    def trace_event(event_id: str, stage: str, label: str, status: str = "running",
                    **fields) -> dict[str, Any]:
        item = {
            "id": event_id,
            "stage": stage,
            "label": label,
            "status": status,
            "summary": "",
            "error": "",
            "elapsed_ms": 0.0,
            **fields,
        }
        return _trace_upsert(trace, item)

    yield ("trace", trace_event("turn-start", "start", "Preparing response", "ok"))

    input_flags = [flag for flag in ctx.guardrail_flags if flag.stage == "input"]
    if input_flags:
        categories = ", ".join(ctx.guardrail_categories) or "flagged text"
        note = (
            f"Part of your message was flagged ({categories}) and was not treated "
            "as an instruction. The question itself was answered."
        )
        notes.append(note)
        yield ("note", {"text": note})
        yield ("trace", trace_event(
            "guardrail-input", "guardrail", "Message screened", "ok", summary=note,
        ))

    if ctx.selection.rejected:
        reasons = ctx.selection.reason_map()
        detail = "; ".join(
            f"{name}: {reasons.get(name, 'not available')}" for name in ctx.selection.rejected
        )
        notes.append(f"Some selected tools were not used ({detail}).")
        yield ("note", {"text": notes[-1]})
        yield ("trace", trace_event(
            "tool-policy", "policy", "Some selected tools were unavailable", "error",
            summary=notes[-1],
        ))

    grounded: Grounded | None = None
    if ctx.capabilities:
        for cap in ctx.capabilities:
            tool = "rag_search" if cap == "rag" else "web_search"
            label = "Searching documents" if cap == "rag" else "Searching the web"
            yield ("trace", trace_event(
                f"retrieval-{tool}", "retrieval", label, "running", tool=tool,
            ))
        try:
            grounded = await grounding.collect(
                ctx.model_text, ctx.capabilities, ctx.target
            )
        except Exception as exc:  # noqa: BLE001 - report retrieval failure honestly
            detail = (redact(f"{type(exc).__name__}: {exc}") or "retrieval failed")[:240]
            for cap in ctx.capabilities:
                tool = "rag_search" if cap == "rag" else "web_search"
                yield ("trace", trace_event(
                    f"retrieval-{tool}", "retrieval",
                    "Document search failed" if cap == "rag" else "Web search failed",
                    "error", tool=tool, error=detail,
                ))
            notes.append(f"Retrieval failed: {detail}")
            grounded = Grounded(
                notes=[notes[-1]],
                refusal=(
                    "I couldn't retrieve the context you asked for, so I have not "
                    f"guessed an answer. {detail}"
                ),
            )
        if grounded.sources:
            yield ("sources", {"sources": grounded.sources})
        for item in grounded.events:
            tool = str(item.get("tool") or "")
            label = str(item.get("label") or "Retrieval")
            yield ("trace", trace_event(
                f"retrieval-{tool}", "retrieval", label,
                str(item.get("status") or "ok"),
                tool=tool,
                summary=str(item.get("summary") or ""),
                error=str(item.get("error") or ""),
                elapsed_ms=float(item.get("elapsed_ms") or 0.0),
            ))
        for note in grounded.notes:
            notes.append(note)
            yield ("note", {"text": note})
        if grounded.injection_hits:
            ctx.injection_hits.extend(grounded.injection_hits)
            yield ("trace", trace_event(
                "guardrail-sources", "guardrail", "Reference text screened", "ok",
                summary=(
                    f"{len(grounded.injection_hits)} instruction-like span(s) found "
                    "in reference data; kept inside the data boundary"
                ),
            ))
        if grounded.refusal:
            done = trace_event(
                "turn-done", "done", "Completed with limitations", "ok",
                summary=grounded.refusal[:240],
                elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
            )
            yield ("trace", done)
            message, conversation = _finalize(
                ctx,
                content=grounded.refusal,
                status="ok",
                usage=None,
                tool_events=[],
                sources=[],
                notes=notes,
                trace=trace,
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
    # The stored text is what the user typed; the model sees the message with
    # injected directives removed (see `screen_input`).
    messages.append({"role": "user", "content": ctx.model_text})

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
            model_event = trace_event(
                f"model-{round_index}",
                "model",
                "Generating response" if round_index == 1 else "Synthesising tool results",
                "running",
                model=ctx.target.model,
            )
            yield ("trace", model_event)
            round_started = time.perf_counter()
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
                finished = {
                    **model_event,
                    "status": "ok",
                    "elapsed_ms": round((time.perf_counter() - round_started) * 1000, 1),
                    "summary": f"{len(round_text)} characters",
                }
                _trace_upsert(trace, finished)
                yield ("trace", finished)
            except Exception as exc:
                if round_index == 1 and schemas and not round_text and not calls:
                    # The endpoint may reject the `tools` field outright. Answer
                    # without tools rather than failing the message.
                    detail = (redact(f"{type(exc).__name__}: {exc}") or "tools unavailable")[:240]
                    yield ("trace", _trace_upsert(trace, {
                        **model_event,
                        "status": "error",
                        "error": detail,
                        "summary": "Tool calling unavailable",
                    }))
                    schemas = []
                    tool_note = NO_TOOL_SUPPORT
                    yield ("note", {"text": tool_note})
                    fallback_event = trace_event(
                        f"model-{round_index}-fallback",
                        "model",
                        "Generating response without tools",
                        "running",
                        model=ctx.target.model,
                    )
                    yield ("trace", fallback_event)
                    fallback_started = time.perf_counter()
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
                    yield ("trace", _trace_upsert(trace, {
                        **fallback_event,
                        "status": "ok",
                        "elapsed_ms": round(
                            (time.perf_counter() - fallback_started) * 1000, 1
                        ),
                        "summary": f"{len(round_text)} characters",
                    }))
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
                    yield ("trace", trace_event(
                        call_id, "tool", event["label"], "error",
                        tool=name, error=result_text, summary="Not enabled",
                    ))
                else:
                    spec = tools.TOOL_SPECS[name]
                    running = trace_event(
                        call_id, "tool", spec.label, "running", tool=name
                    )
                    yield ("trace", running)
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
                    # A tool result is external content: validate it before the
                    # model sees it, and record what was wrong if anything was.
                    tool_ok, tool_flags = validate_tool_output(name, result_text)
                    if tool_flags:
                        ctx.guardrail_flags.extend(tool_flags)
                        # Keep the attacker's phrasing so the output screen can
                        # tell whether the answer repeated it.
                        ctx.injection_hits.extend(scan_injection(result_text)[:5])
                    if not tool_ok:
                        note = (
                            f"The {spec.label.lower()} result was flagged by the "
                            "guardrails and is being used as data only."
                        )
                        notes.append(note)
                        yield ("note", {"text": note})
                        yield ("trace", trace_event(
                            f"guardrail-{call_id}", "guardrail",
                            "Tool result screened", "error",
                            tool=name,
                            summary=", ".join(flag.rule for flag in tool_flags)[:240],
                        ))
                    done = {
                        **running,
                        "status": event.get("status") or "ok",
                        "summary": event.get("summary") or "",
                        "error": event.get("error") or "",
                        "elapsed_ms": event.get("elapsed_ms") or 0.0,
                    }
                    _trace_upsert(trace, done)
                    yield ("trace", done)
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
            yield ("trace", trace_event(
                "tool-limit", "limit", tool_note, "error", summary=tool_note,
            ))
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

        label = (
            "Completed" if status == "ok" and not tool_note
            else "Completed with limitations" if status == "ok"
            else "Stopped" if status == "stopped"
            else "Failed"
        )
        done_status = "ok" if status == "ok" else "stopped" if status == "stopped" else "error"
        yield ("trace", trace_event(
            "turn-done", "done", label, done_status,
            summary=(error or tool_note or "")[:240],
            elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
        ))
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
            trace=trace,
        )
        yield ("done", {"message": message, "conversation": conversation})

    except asyncio.CancelledError:
        trace_event(
            "turn-done", "done", "Stopped", "stopped",
            elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
        )
        _finalize(
            ctx,
            content=all_text.strip(),
            status="stopped",
            usage=None,
            tool_events=tool_events,
            sources=sources,
            tool_note=tool_note,
            notes=notes,
            trace=trace,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - after headers this must be an event
        detail = (redact(f"{type(exc).__name__}: {exc}") or "provider error")[:300]
        yield ("trace", trace_event(
            "turn-done", "done", "Failed", "error",
            error=detail,
            elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
        ))
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
            trace=trace,
        )
        yield ("error", {"detail": detail, "code": "chat_failed"})
        yield ("done", {"message": message, "conversation": conversation})
