"""Direct LiteLLM streaming for chat, behind a small testable seam."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator

from athena.chat.models import ChatTarget, LLMDelta
from athena.config.endpoint_guard import expected_host_scope, install_guard
from athena.config.ratelimit import estimate_tokens, get_limiter, get_output_limiter
from athena.observability.litellm_usage import usage_from_response

MAX_TOKENS = int(os.getenv("ATHENA_CHAT_MAX_TOKENS") or 3000)
_GUARDED = False


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _tool_fragment(call: Any) -> LLMDelta:
    function = _get(call, "function", {}) or {}
    arguments = _get(function, "arguments", "") or ""
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return LLMDelta(
        tool_index=int(_get(call, "index", 0) or 0),
        tool_id=str(_get(call, "id", "") or ""),
        tool_name=str(_get(function, "name", "") or ""),
        tool_arguments=str(arguments),
    )


def _request_text(messages: list[dict[str, Any]]) -> str:
    try:
        return json.dumps(messages, ensure_ascii=False, default=str)
    except ValueError:
        return str(messages)


def _unsupported_stream_options(error: Exception) -> bool:
    """Only retry an explicit pre-stream 400/422 rejection of this field."""
    response = _get(error, "response")
    status = _get(error, "status_code", _get(response, "status_code"))
    if status not in (400, 422):
        return False

    body = _get(error, "body", {})
    details = body.get("error", body) if isinstance(body, dict) else {}
    param = _get(error, "param") or _get(details, "param")
    code = _get(error, "code") or _get(details, "code")
    message = _get(details, "message") or str(error)
    text = str(message).lower()
    stream_option_param = (
        param == "stream_options"
        or isinstance(param, str) and param.startswith("stream_options.")
    )
    if stream_option_param and str(code).lower() in {
        "unsupported_parameter",
        "unknown_parameter",
    }:
        return True
    if "stream_options" not in text:
        return False
    return any(
        phrase in text
        for phrase in (
            "unsupported",
            "not supported",
            "unrecognized",
            "unknown parameter",
            "unknown field",
            "unexpected",
            "not allowed",
            "additional properties",
            "extra field",
            "extra input",
        )
    )


async def stream_completion(
    target: ChatTarget,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = MAX_TOKENS,
) -> AsyncIterator[LLMDelta]:
    """Yield normalised text/tool/usage deltas from one completion call."""
    global _GUARDED
    if not _GUARDED:
        install_guard()
        _GUARDED = True

    import litellm

    reserve = max(256, int(max_tokens or MAX_TOKENS))
    estimate = estimate_tokens(_request_text(messages))
    limiter = get_limiter()
    output = get_output_limiter()
    await asyncio.to_thread(limiter.acquire, estimate, "chat")
    await asyncio.to_thread(output.acquire, reserve, "chat")

    kwargs: dict[str, Any] = {
        "model": target.model,
        "messages": messages,
        "max_tokens": reserve,
        "temperature": 0.3,
        "timeout": float(os.getenv("ATHENA_LLM_TIMEOUT_S") or 120),
        "stream": True,
        "drop_params": True,
    }
    if target.api_key:
        kwargs["api_key"] = target.api_key
    else:
        # Local OpenAI-compatible servers often need no key, but the SDK still
        # requires a non-empty credential value. It never leaves the machine.
        kwargs["api_key"] = "local-no-key"
    if target.api_base:
        kwargs["api_base"] = target.api_base
    if target.reasoning_effort:
        kwargs["reasoning_effort"] = target.reasoning_effort
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    tokens_in = 0
    tokens_out = 0
    reasoning = 0
    try:
        with expected_host_scope(target.api_base):
            try:
                response = await litellm.acompletion(
                    **kwargs, stream_options={"include_usage": True}
                )
            except Exception as exc:
                # Retry only a clear unsupported-field response before streaming.
                if not _unsupported_stream_options(exc):
                    raise
                response = await litellm.acompletion(**kwargs)

            async for chunk in response:
                usage_in, usage_out, usage_reasoning = usage_from_response(chunk)
                if usage_in or usage_out:
                    tokens_in = usage_in or tokens_in
                    tokens_out = usage_out or tokens_out
                    reasoning = usage_reasoning or reasoning
                    yield LLMDelta(
                        usage={
                            "tokens_in": tokens_in,
                            "tokens_out": tokens_out,
                            "reasoning_tokens": reasoning,
                        }
                    )

                choices = _get(chunk, "choices", []) or []
                if not choices:
                    continue
                choice = choices[0]
                delta = _get(choice, "delta", None) or _get(choice, "message", {}) or {}
                content = _get(delta, "content", "") or ""
                if content:
                    yield LLMDelta(text=str(content))
                for call in _get(delta, "tool_calls", []) or []:
                    yield _tool_fragment(call)
                finish = _get(choice, "finish_reason", "") or ""
                if finish:
                    yield LLMDelta(finish_reason=str(finish))
    finally:
        limiter.record(tokens_in or estimate)
        output.record(tokens_out or reserve)
