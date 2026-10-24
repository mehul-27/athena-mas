"""Model wrappers that put every call behind the shared token limiter.

ADK goes through ``LiteLlm.generate_content_async``; CrewAI goes through
``LLM.call`` / ``LLM.acall``. Those are the two narrowest points both frameworks
share, so instrumenting them covers every model call without touching agent
code.

Reasoning models spend output tokens on hidden reasoning, so a call can come
back with no visible text and ``finish_reason == "length"``. That is retried
once with the output budget doubled (capped at MAX_RETRY_TOKENS) before it
counts as a failure.
"""

from __future__ import annotations

import json
from google.adk.models.lite_llm import LiteLLMClient, LiteLlm

from athena.config.usage import last_reasoning, reasoning_from_usage, set_last_reasoning
from athena.config.ratelimit import estimate_tokens, get_limiter, get_output_limiter
from athena.config.reply_guard import (
    doubled,
    empty_and_truncated,
    has_text,
    note_empty_reply,
)
from athena.observability.context import current_agent

def _request_text(llm_request) -> str:
    try:
        return llm_request.model_dump_json()
    except Exception:  # noqa: BLE001 - estimation must never break a call
        return str(llm_request)


def _messages_text(messages) -> str:
    if isinstance(messages, str):
        return messages
    try:
        return json.dumps(messages, default=str)
    except ValueError:
        return str(messages)


class UsageCapturingClient(LiteLLMClient):
    """LiteLLM client that remembers the raw usage of the last call."""

    async def acompletion(self, model, messages, tools, **kwargs):
        response = await super().acompletion(
            model=model, messages=messages, tools=tools, **kwargs
        )
        set_last_reasoning(reasoning_from_usage(getattr(response, "usage", None)))
        return response


class RateLimitedLiteLlm(LiteLlm):
    """LiteLlm that reserves budget, retries an empty reply once, settles usage."""

    async def _run(self, llm_request, stream: bool):
        return [item async for item in super().generate_content_async(llm_request, stream)]

    def _capture_usage(self) -> None:
        """Install the usage-capturing client once (a declared LiteLlm field)."""
        if not isinstance(self.llm_client, UsageCapturingClient):
            self.llm_client = UsageCapturingClient()

    async def generate_content_async(self, llm_request, stream: bool = False):
        limiter = get_limiter()
        self._capture_usage()
        set_last_reasoning(None)
        output = get_output_limiter()
        reserve = int(self._additional_args.get("max_tokens") or 0)
        estimate = estimate_tokens(_request_text(llm_request))
        limiter.acquire(estimate, agent=current_agent())
        output.acquire(reserve, agent=current_agent())

        responses = await self._run(llm_request, stream)

        if empty_and_truncated(responses):
            note_empty_reply("adk", reserve)
            # The retry asks for a bigger output budget, so reserve the delta:
            # otherwise the limiter under-counts exactly when the model is
            # burning tokens fastest.
            output.acquire(doubled(reserve) - reserve, agent=current_agent())
            original = self._additional_args.get("max_tokens")
            self._additional_args["max_tokens"] = doubled(reserve)
            try:
                retried = await self._run(llm_request, stream)
            finally:
                if original is None:
                    self._additional_args.pop("max_tokens", None)
                else:
                    self._additional_args["max_tokens"] = original
            responses = retried
            if not any(has_text(r) for r in responses):
                note_empty_reply("adk", doubled(reserve))

        tokens_in = tokens_out = None
        for response in responses:
            usage = getattr(response, "usage_metadata", None)
            if usage is None:
                continue
            tokens_in = getattr(usage, "prompt_token_count", None) or tokens_in
            tokens_out = getattr(usage, "candidates_token_count", None) or tokens_out
            # Some gateways report reasoning at the top level, which ADK drops;
            # put it where the monitoring plugin reads it.
            if getattr(usage, "thoughts_token_count", None) is None:
                recovered = reasoning_from_usage(usage) or last_reasoning()
                if recovered:
                    try:
                        usage.thoughts_token_count = recovered
                    except Exception:  # noqa: BLE001 - never break a call for metrics
                        pass

        limiter.record(tokens_in or estimate)
        output.record(tokens_out or reserve)

        for response in responses:
            yield response


def make_crewai_llm_class():
    """Build the CrewAI LLM subclass (imported lazily: crewai is heavy)."""
    from crewai import LLM
    from crewai.llms.cache import CACHE_BREAKPOINT_KEY

    class RateLimitedCrewLLM(LLM):
        def _format_messages_for_provider(self, messages):
            # CrewAI 1.15.28 leaks an internal prompt-cache marker to strict
            # providers (Groq rejects it); strip it before the request leaves.
            cleaned = [
                {key: value for key, value in msg.items() if key != CACHE_BREAKPOINT_KEY}
                for msg in messages
            ]
            return super()._format_messages_for_provider(cleaned)

        @staticmethod
        def _kwargs(messages, tools, callbacks, available_functions, from_task,
                    from_agent, response_model) -> dict:
            return {
                "messages": messages, "tools": tools, "callbacks": callbacks,
                "available_functions": available_functions, "from_task": from_task,
                "from_agent": from_agent, "response_model": response_model,
            }

        def call(self, messages, tools=None, callbacks=None, available_functions=None,
                 from_task=None, from_agent=None, response_model=None):
            limiter = get_limiter()
            output = get_output_limiter()
            reserve = int(self.max_tokens or 0)
            estimate = estimate_tokens(_messages_text(messages))
            limiter.acquire(estimate, agent=current_agent())
            output.acquire(reserve, agent=current_agent())
            kwargs = self._kwargs(messages, tools, callbacks, available_functions,
                                  from_task, from_agent, response_model)
            parent = super()
            try:
                result = parent.call(**kwargs)
                if isinstance(result, str) and not result.strip():
                    note_empty_reply("crewai", self.max_tokens)
                    output.acquire(doubled(reserve) - reserve, agent=current_agent())
                    original = self.max_tokens
                    self.max_tokens = doubled(original)
                    try:
                        result = parent.call(**kwargs)
                    finally:
                        self.max_tokens = original
                return result
            finally:
                limiter.record(estimate)
                output.record(reserve)

        async def acall(self, messages, tools=None, callbacks=None, available_functions=None,
                        from_task=None, from_agent=None, response_model=None):
            limiter = get_limiter()
            output = get_output_limiter()
            reserve = int(self.max_tokens or 0)
            estimate = estimate_tokens(_messages_text(messages))
            limiter.acquire(estimate, agent=current_agent())
            output.acquire(reserve, agent=current_agent())
            kwargs = self._kwargs(messages, tools, callbacks, available_functions,
                                  from_task, from_agent, response_model)
            try:
                result = await super().acall(**kwargs)
                if isinstance(result, str) and not result.strip():
                    note_empty_reply("crewai", self.max_tokens)
                    output.acquire(doubled(reserve) - reserve, agent=current_agent())
                    original = self.max_tokens
                    self.max_tokens = doubled(original)
                    try:
                        result = await super().acall(**kwargs)
                    finally:
                        self.max_tokens = original
                return result
            finally:
                limiter.record(estimate)
                output.record(reserve)

    return RateLimitedCrewLLM
