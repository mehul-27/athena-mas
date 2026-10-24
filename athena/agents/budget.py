"""Per-agent budgets that actually stop a worker's loop.

The old guard returned a dict from ``before_tool_callback``; ADK treats any
returned mapping as a *successful* tool result, so the model saw "budget
reached" as an answer and called the tool again. This module bounds the worker
on both axes and, crucially, removes the tools from the next model request and
short-circuits the model once the budget is gone.

One Budget per agent instance, so parallel workers never share counters.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

DEFAULT_MAX_TOOL_CALLS = 3
DEFAULT_MAX_MODEL_CALLS = 6
STOP_TEXT = "Budget reached; findings are recorded on the blackboard."

DUPLICATE_RESULT = {
    "duplicate": True,
    "note": "Already executed; its result is recorded. Do not repeat it. "
    "Write your final answer now.",
}


def _limit(name: str, default: int) -> int:
    value = (os.getenv(name) or "").strip()
    return int(value) if value.isdigit() else default


def budget_limits() -> tuple[int, int]:
    return (
        _limit("ATHENA_MAX_TOOL_CALLS", DEFAULT_MAX_TOOL_CALLS),
        _limit("ATHENA_WORKER_MAX_MODEL_CALLS", DEFAULT_MAX_MODEL_CALLS),
    )


def _normalise(value):
    """Collapse and strip whitespace in strings, recursively."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value


def call_key(tool_name: str, args) -> str:
    """Stable identity for a tool call: whitespace and key order do not matter."""
    try:
        normalised = json.dumps(_normalise(args), sort_keys=True, default=str)
    except ValueError:
        normalised = str(args)
    return f"{tool_name}:{normalised}"


@dataclass
class Budget:
    """Counts and decisions for one worker."""

    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS
    tool_calls: int = 0
    model_calls: int = 0
    stop_reason: str = ""
    seen: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls) -> "Budget":
        tool_limit, model_limit = budget_limits()
        return cls(max_tool_calls=tool_limit, max_model_calls=model_limit)

    def before_tool(self, tool, args, tool_context):
        """None means 'run it'; a dict is returned to the model instead."""
        name = getattr(tool, "name", "")
        key = call_key(name, args)
        if key in self.seen:
            self.stop_reason = self.stop_reason or "duplicate"
            return dict(DUPLICATE_RESULT)
        if self.tool_calls >= self.max_tool_calls:
            self.stop_reason = self.stop_reason or "tool_budget"
            return {"budget": "tool_calls", "note": STOP_TEXT}
        self.seen.add(key)
        self.tool_calls += 1
        return None

    def before_model(self, callback_context, llm_request):
        """Trim tools, then cut the call short when the model budget is gone."""
        self.model_calls += 1
        if self.model_calls > self.max_model_calls:
            self.stop_reason = self.stop_reason or "model_budget"
            return _canned_response()

        if self.tool_calls >= self.max_tool_calls and getattr(llm_request, "config", None):
            # The model can no longer call anything: it must answer in text.
            self.stop_reason = self.stop_reason or "tool_budget"
            llm_request.config.tools = []
        return None

    def as_detail(self) -> dict:
        return {
            "tool_calls": self.tool_calls,
            "model_calls": self.model_calls,
            "stop_reason": self.stop_reason or "answered",
        }


def _canned_response():
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=STOP_TEXT)])
    )


def make_budget_callbacks(budget: Budget, recorder=None, agent: str = ""):
    """(before_model, before_tool) for one agent, with a stop event."""
    def before_model(callback_context, llm_request):
        response = budget.before_model(callback_context, llm_request)
        if response is not None:
            _emit_stop(recorder, agent)
        return response

    def before_tool(tool, args, tool_context):
        result = budget.before_tool(tool, args, tool_context)
        if result is not None and budget.stop_reason:
            _emit_stop(recorder, agent)
        return result

    return before_model, before_tool


def _emit_stop(recorder, agent: str) -> None:
    if recorder is None:
        return
    recorder.emit(
        "error",
        agent,
        "adk",
        ok=False,
        error_type="budget_stop",
        error_message=agent and f"{agent}: {STOP_TEXT}" or STOP_TEXT,
        payload_summary="budget_stop",
    )
