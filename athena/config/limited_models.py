"""Model wrappers that put every call behind the shared token limiter.

ADK goes through ``LiteLlm.generate_content_async``; CrewAI goes through
``LLM.call`` / ``LLM.acall``. Those are the two narrowest points both frameworks
share, so instrumenting them covers every model call without touching agent
code.
"""

from __future__ import annotations

import json

from google.adk.models.lite_llm import LiteLlm

from athena.config.ratelimit import estimate_tokens, get_limiter, get_output_limiter
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


class RateLimitedLiteLlm(LiteLlm):
    """LiteLlm that reserves budget before the call and settles after it."""

    async def generate_content_async(self, llm_request, stream: bool = False):
        limiter = get_limiter()
        output = get_output_limiter()
        reserve = int(self._additional_args.get("max_tokens") or 0)
        estimate = estimate_tokens(_request_text(llm_request))
        limiter.acquire(estimate, agent=current_agent())
        output.acquire(reserve, agent=current_agent())

        reported = False
        completion = None
        async for response in super().generate_content_async(llm_request, stream):
            usage = getattr(response, "usage_metadata", None)
            actual = getattr(usage, "prompt_token_count", None) if usage else None
            if actual and not reported:
                limiter.record(actual)
                reported = True
            if usage is not None:
                completion = getattr(usage, "candidates_token_count", None) or completion
            yield response

        if not reported:
            limiter.record(estimate)
        output.record(completion or reserve)


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

        def call(self, messages, tools=None, callbacks=None, available_functions=None,
                 from_task=None, from_agent=None, response_model=None):
            limiter = get_limiter()
            output = get_output_limiter()
            reserve = int(self.max_tokens or 0)
            estimate = estimate_tokens(_messages_text(messages))
            limiter.acquire(estimate, agent=current_agent())
            output.acquire(reserve, agent=current_agent())
            try:
                return super().call(
                    messages, tools=tools, callbacks=callbacks,
                    available_functions=available_functions, from_task=from_task,
                    from_agent=from_agent, response_model=response_model,
                )
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
            try:
                return await super().acall(
                    messages, tools=tools, callbacks=callbacks,
                    available_functions=available_functions, from_task=from_task,
                    from_agent=from_agent, response_model=response_model,
                )
            finally:
                limiter.record(estimate)
                output.record(reserve)

    return RateLimitedCrewLLM
