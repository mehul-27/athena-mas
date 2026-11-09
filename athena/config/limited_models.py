"""Model wrappers that put every call behind the shared token limiter.

ADK goes through ``LiteLlm.generate_content_async``; CrewAI goes through
``LLM.call`` / ``LLM.acall``. Those are the two narrowest points both frameworks
share, so instrumenting them covers every model call without touching agent
code.

Reasoning models spend output tokens on hidden reasoning, so a call can come
back with no visible text and ``finish_reason == "length"``. That is retried
once with the output budget doubled (capped at MAX_RETRY_TOKENS) before it
counts as a failure.

A provider can also *reject* a tool call before it ever reaches us
(``tool_use_failed`` / an unknown tool name / arguments that do not match the
schema). This is the narrowest point at which that call can actually be made
again, so it is handled here: one bounded retry carrying a corrective
instruction that lists the exact permitted tools, then a degraded response the
agent can continue from. See :mod:`athena.config.tool_call_guard`.
"""

from __future__ import annotations

import json
from google.adk.models.lite_llm import LiteLLMClient, LiteLlm

from athena.config import tool_call_guard
from athena.config.usage import last_reasoning, reasoning_from_usage, set_last_reasoning
from athena.config.ratelimit import estimate_tokens, get_limiter, get_output_limiter
from athena.config.reply_guard import (
    doubled,
    empty_and_truncated,
    has_text,
    note_empty_reply,
)
from athena.observability.context import current_agent
from athena.observability.retry_events import note_retry, retry_empty_reply

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


def _reserve_tokens(
    limiter, output, input_tokens: int, output_tokens: int, agent: str
) -> None:
    """Reserve both budgets; release input only if output reservation fails pre-call."""
    limiter.acquire(input_tokens, agent=agent)
    try:
        output.acquire(output_tokens, agent=agent)
    except BaseException:
        # No provider request was sent. Keep any earlier output reservation intact.
        limiter.record(0)
        raise


def _settle_estimates(limiter, output, input_tokens: int, output_tokens: int) -> None:
    """Settle conservative usage when a provider attempt fails without usage data."""
    limiter.record(input_tokens)
    output.record(output_tokens)


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
        _reserve_tokens(limiter, output, estimate, reserve, current_agent())
        reserved_output = reserve
        reservation_active = True

        try:
            responses = await self._run(llm_request, stream)
        except BaseException as exc:
            _settle_estimates(limiter, output, estimate, reserve)
            reservation_active = False
            # A provider-side rejection of the *tool call* is one bounded
            # degradation, not the end of the run: the evidence already on the
            # blackboard must still reach the writer. Everything else keeps
            # propagating untouched.
            if not isinstance(exc, Exception) or not tool_call_guard.is_tool_call_validation_error(
                exc
            ):
                raise
            responses = await self._retry_rejected_tool_call(
                llm_request, stream, exc, limiter, output, estimate, reserve
            )
            if responses is None:
                return

        if empty_and_truncated(responses):
            note_empty_reply("adk", reserve)
            # Keep this attempt's estimate once. A corrective-tool retry has
            # already settled its own reservation before reaching this branch.
            if reservation_active:
                _settle_estimates(limiter, output, estimate, reserve)
                reservation_active = False
            # This is a second provider request, so reserve its full output cap.
            retried_budget = doubled(reserve)
            retry_reserve = retried_budget
            _reserve_tokens(limiter, output, estimate, retry_reserve, current_agent())
            reserved_output = retry_reserve
            reservation_active = True
            # One retry happens below, so exactly one ``retry`` event is written
            # here — before the second call, because the retry is a fact by now.
            retry_empty_reply("adk", reserve, retried_budget)
            original = self._additional_args.get("max_tokens")
            self._additional_args["max_tokens"] = retried_budget
            try:
                try:
                    retried = await self._run(llm_request, stream)
                except BaseException:
                    _settle_estimates(limiter, output, estimate, retry_reserve)
                    reservation_active = False
                    raise
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

        if reservation_active:
            limiter.record(tokens_in or estimate)
            output.record(tokens_out or reserved_output)

        for response in responses:
            yield response

    async def _retry_rejected_tool_call(
        self,
        llm_request,
        stream: bool,
        error: BaseException,
        limiter,
        output,
        estimate: int,
        reserve: int,
    ) -> list | None:
        """Exactly one corrective retry for a rejected tool call.

        Returns the retried responses, or ``None`` when even the retry was
        rejected: the caller then yields the degraded response instead of
        raising, so the stage finishes and the writer can still produce a
        (possibly qualified) report from the evidence already gathered.

        One retry, one ``retry`` event, and only after the decision to retry —
        a rejection that is not retried never claims one.
        """
        agent = current_agent()
        correction = tool_call_guard.corrective_message(error, llm_request)
        retry_request = tool_call_guard.with_corrective_message(llm_request, correction)
        retry_estimate = estimate_tokens(_request_text(retry_request))
        # The retry is a real model call: reserve its budget like any other, so
        # the shared limiter and the per-run caps still see it.
        try:
            _reserve_tokens(limiter, output, retry_estimate, reserve, agent)
        except BaseException:
            tool_call_guard.record_rejection(error, agent, will_retry=False, attempt=2)
            raise
        tool_call_guard.record_rejection(error, agent, will_retry=True, attempt=2)
        note_retry(
            agent,
            "provider rejected a tool call; retrying once with the permitted tools listed",
            "tool_call",
            "adk",
            attempt=2,
            of=2,
            stage="model_call",
            error_type="tool_use_failed",
            rejected_tool=tool_call_guard.offending_tool(error),
        )

        try:
            responses = await self._run(retry_request, stream)
        except BaseException as exc:
            _settle_estimates(limiter, output, retry_estimate, reserve)
            if not isinstance(exc, Exception) or not tool_call_guard.is_tool_call_validation_error(
                exc
            ):
                raise
            # Second rejection: stop calling tools, keep the run alive.
            tool_call_guard.record_rejection(exc, agent, will_retry=False, attempt=2)
            tool_call_guard.record_degraded(exc, agent)
            return [tool_call_guard.degraded_response(exc)]

        # The retry itself happened, so its cost is measured, not estimated.
        usage_in = usage_out = None
        for response in responses:
            usage = getattr(response, "usage_metadata", None)
            if usage is None:
                continue
            usage_in = getattr(usage, "prompt_token_count", None) or usage_in
            usage_out = getattr(usage, "candidates_token_count", None) or usage_out
        limiter.record(usage_in or retry_estimate)
        output.record(usage_out or reserve)
        return responses


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
            _reserve_tokens(limiter, output, estimate, reserve, current_agent())
            kwargs = self._kwargs(messages, tools, callbacks, available_functions,
                                  from_task, from_agent, response_model)
            parent = super()
            reserved_output = reserve
            reservation_active = True
            try:
                result = parent.call(**kwargs)
                if isinstance(result, str) and not result.strip():
                    note_empty_reply("crewai", self.max_tokens)
                    _settle_estimates(limiter, output, estimate, reserve)
                    reservation_active = False
                    retried_budget = doubled(reserve)
                    retry_reserve = retried_budget
                    _reserve_tokens(limiter, output, estimate, retry_reserve, current_agent())
                    reserved_output = retry_reserve
                    reservation_active = True
                    # The retry below is real, so report it (once) here.
                    retry_empty_reply("crewai", self.max_tokens, retried_budget)
                    original = self.max_tokens
                    self.max_tokens = retried_budget
                    try:
                        result = parent.call(**kwargs)
                    finally:
                        self.max_tokens = original
                return result
            finally:
                if reservation_active:
                    limiter.record(estimate)
                    output.record(reserved_output)

        async def acall(self, messages, tools=None, callbacks=None, available_functions=None,
                        from_task=None, from_agent=None, response_model=None):
            limiter = get_limiter()
            output = get_output_limiter()
            reserve = int(self.max_tokens or 0)
            estimate = estimate_tokens(_messages_text(messages))
            _reserve_tokens(limiter, output, estimate, reserve, current_agent())
            kwargs = self._kwargs(messages, tools, callbacks, available_functions,
                                  from_task, from_agent, response_model)
            reserved_output = reserve
            reservation_active = True
            try:
                result = await super().acall(**kwargs)
                if isinstance(result, str) and not result.strip():
                    note_empty_reply("crewai", self.max_tokens)
                    _settle_estimates(limiter, output, estimate, reserve)
                    reservation_active = False
                    retried_budget = doubled(reserve)
                    retry_reserve = retried_budget
                    _reserve_tokens(limiter, output, estimate, retry_reserve, current_agent())
                    reserved_output = retry_reserve
                    reservation_active = True
                    # The retry below is real, so report it (once) here.
                    retry_empty_reply("crewai", self.max_tokens, retried_budget)
                    original = self.max_tokens
                    self.max_tokens = retried_budget
                    try:
                        result = await super().acall(**kwargs)
                    finally:
                        self.max_tokens = original
                return result
            finally:
                if reservation_active:
                    limiter.record(estimate)
                    output.record(reserved_output)

    return RateLimitedCrewLLM
