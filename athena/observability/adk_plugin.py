"""The ADK plugin that turns ADK's callbacks into events.

Chosen over per-agent callbacks: a plugin applies to every agent the runner
executes — including the custom supervisor and the A2A stage agents, which are
plain ``BaseAgent``s and would each need their own callbacks. One registration
on the runner covers all of them, which is what the ADK docs recommend for
logging/monitoring.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from athena.observability.context import reset_agent, set_agent
from athena.observability.recorder import Recorder, get_recorder


def _summary(value: Any, limit: int = 160) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


class MonitoringPlugin(BasePlugin):
    """Emits stage, model, tool and error events for every agent in the runner."""

    def __init__(self, recorder: Recorder | None = None) -> None:
        super().__init__(name="athena_monitoring")
        self._recorder = recorder
        self._starts: dict[str, float] = {}
        self._tokens: dict[str, list[int]] = {}
        self._args: dict[str, str] = {}

    # --- helpers --------------------------------------------------------
    def _rec(self) -> Recorder | None:
        return self._recorder or get_recorder()

    def _emit(self, kind: str, agent: str, **fields) -> None:
        recorder = self._rec()
        if recorder is not None:
            recorder.emit(kind, agent, "adk", **fields)

    # --- agent lifecycle ------------------------------------------------
    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Optional[types.Content]:
        self._starts[agent.name] = time.perf_counter()
        self._tokens[agent.name] = [0, 0]
        set_agent(agent.name)
        self._emit("stage_start", agent.name)
        return None

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Optional[types.Content]:
        started = self._starts.pop(agent.name, None)
        tokens_in, tokens_out = self._tokens.pop(agent.name, [0, 0])
        duration = round((time.perf_counter() - started) * 1000, 2) if started else None
        self._emit(
            "stage_end",
            agent.name,
            duration_ms=duration,
            tokens_in=tokens_in or None,
            tokens_out=tokens_out or None,
        )
        return None

    # --- model calls ----------------------------------------------------
    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response
    ) -> Optional[Any]:
        usage = getattr(llm_response, "usage_metadata", None)
        tokens_in = getattr(usage, "prompt_token_count", None) if usage else None
        tokens_out = getattr(usage, "candidates_token_count", None) if usage else None
        agent = callback_context.agent_name if hasattr(callback_context, "agent_name") else "supervisor"
        counters = self._tokens.get(agent)
        if counters is not None:
            counters[0] += tokens_in or 0
            counters[1] += tokens_out or 0
        # ADK maps litellm's completion_tokens_details.reasoning_tokens onto
        # thoughts_token_count, so that is the reasoning signal here.
        reasoning = getattr(usage, "thoughts_token_count", None) if usage else None
        self._emit(
            "model_call", agent, tokens_in=tokens_in, tokens_out=tokens_out,
            extra={"reasoning_tokens": reasoning},
        )
        return None

    async def on_model_error_callback(
        self, callback_context: CallbackContext, llm_request, error: Exception
    ):
        agent = getattr(callback_context, "agent_name", "supervisor")
        self._emit(
            "error",
            agent,
            ok=False,
            error_type=type(error).__name__,
            error_message=str(error),
            payload_summary="model call failed",
        )
        return None

    # --- tool calls -----------------------------------------------------
    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context
    ) -> Optional[dict]:
        # Only the start time is recorded here; the event itself is emitted in
        # after_tool_callback, where duration and outcome are known.
        self._starts[f"tool:{tool.name}"] = time.perf_counter()
        self._args[f"tool:{tool.name}"] = _summary(tool_args)
        return None

    async def after_tool_callback(
        self, *, tool: BaseTool, tool_args: dict, tool_context, result: dict
    ) -> Optional[dict]:
        started = self._starts.pop(f"tool:{tool.name}", None)
        error = None
        if isinstance(result, dict) and (result.get("isError") or result.get("error")):
            error = str(result.get("error") or "tool returned isError")
        self._emit(
            "tool_call",
            getattr(tool_context, "agent_name", "researcher"),
            duration_ms=round((time.perf_counter() - started) * 1000, 2) if started else None,
            ok=error is None,
            error_type="ToolError" if error else None,
            error_message=error,
            payload_summary=f"{tool.name}: {self._args.pop(f'tool:{tool.name}', '')}",
        )
        return None
