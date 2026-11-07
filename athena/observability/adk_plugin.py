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

from athena.observability.context import set_agent
from athena.config.usage import last_reasoning, reasoning_from_usage
from athena.observability.recorder import Recorder, get_recorder
from athena.tools.web.urls import plain_http_url


def _summary(value: Any, limit: int = 160) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _source_rows(tool_name: str, result: Any) -> list[dict]:
    """Compact source labels for the activity trace, never the full payload."""
    if not isinstance(result, dict):
        return []
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return []
    rows: list[dict] = []
    if tool_name == "web_search":
        for hit in (structured.get("results") or [])[:3]:
            if isinstance(hit, dict):
                rows.append({"title": hit.get("title") or "", "url": hit.get("url") or ""})
    elif tool_name == "rag_search":
        for hit in (structured.get("hits") or [])[:3]:
            if isinstance(hit, dict):
                rows.append({
                    "document": hit.get("document") or "",
                    "page": hit.get("page"),
                })
    elif tool_name == "fetch_page":
        rows.append({
            "title": structured.get("title") or "",
            "url": (
                structured.get("final_url")
                or structured.get("requested_url")
                or ""
            ),
            "status": structured.get("status") or "",
        })
    return [row for row in rows if any(value for value in row.values())]


class MonitoringPlugin(BasePlugin):
    """Emits stage, model, tool and error events for every agent in the runner."""

    def __init__(self, recorder: Recorder | None = None) -> None:
        super().__init__(name="athena_monitoring")
        self._recorder = recorder
        self._starts: dict[str, float] = {}
        self._tokens: dict[str, list[int]] = {}
        self._args: dict[str, str] = {}
        # Retry detection: ADK exposes no retry callback, but it does tell us the
        # outcome, so a successful call that follows a failed one for the same
        # agent (and the same tool) is a retry attempt. Marked ``inferred`` in
        # the event, so a deduction is never confused with a reported fact.
        self._model_failures: dict[str, int] = {}
        self._tool_failures: dict[tuple[str, str], int] = {}
        self._handoffs: set[str] = set()

    # --- helpers --------------------------------------------------------
    def _rec(self) -> Recorder | None:
        return self._recorder or get_recorder()

    def _emit(self, kind: str, agent: str, **fields) -> None:
        recorder = self._rec()
        if recorder is not None:
            recorder.emit(kind, agent, "adk", **fields)

    # --- agent lifecycle ------------------------------------------------
    @staticmethod
    def _stage_key(agent: BaseAgent) -> str:
        # Parallel pools can contain two agents with the same display name
        # (the AssignedWorker wrapper and its inner LlmAgent). Keying only by
        # name overwrites the first start and leaves an orphan stage_start.
        return f"stage:{agent.name}:{id(agent)}"

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Optional[types.Content]:
        self._starts[self._stage_key(agent)] = time.perf_counter()
        self._starts[f"stage-name:{agent.name}"] = time.perf_counter()
        self._tokens.setdefault(agent.name, [0, 0])
        set_agent(agent.name)
        self._emit("stage_start", agent.name, extra={"stage_scope": "adk-wrapper"})
        self._emit_handoff(agent)
        return None

    def _emit_handoff(self, agent: BaseAgent) -> None:
        """One ``handoff`` event when an A2A stage agent takes over.

        ``A2AStageAgent`` is duck-typed (``role`` + ``card_url``) instead of
        imported, so this module keeps no dependency on the agent package and a
        plain ``LlmAgent`` can never trip the check. The event is emitted once
        per agent name, in addition to the ``a2a_call`` that carries the bytes:
        the handoff is the *decision*, the call is its traffic.
        """
        role = getattr(agent, "role", None)
        card_url = getattr(agent, "card_url", None)
        if not role or card_url is None or agent.name in self._handoffs:
            return
        self._handoffs.add(agent.name)
        self._emit(
            "handoff",
            agent.name,
            extra={
                "via": "a2a",
                "role": str(role),
                "to": str(role),
                "from": "adk",
                "to_framework": "crewai",
            },
        )

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Optional[types.Content]:
        started = self._starts.pop(self._stage_key(agent), None)
        name_started = self._starts.pop(f"stage-name:{agent.name}", None)
        if started is None:
            started = name_started
        tokens_in, tokens_out = self._tokens.pop(agent.name, [0, 0])
        duration = round((time.perf_counter() - started) * 1000, 2) if started else None
        self._emit(
            "stage_end",
            agent.name,
            duration_ms=duration,
            tokens_in=tokens_in or None,
            tokens_out=tokens_out or None,
            extra={"stage_scope": "adk-wrapper"},
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
        reasoning = None
        if usage:
            reasoning = (
                getattr(usage, "thoughts_token_count", None)
                or reasoning_from_usage(usage)
                or last_reasoning()
            )
        failed = self._model_failures.pop(agent, 0)
        self._emit(
            "model_call", agent, tokens_in=tokens_in, tokens_out=tokens_out,
            extra={
                "reasoning_tokens": reasoning,
                # Recorded so cost can be priced later without guessing a model.
                "model": getattr(llm_response, "model_version", None) or None,
                "retry_attempt": failed or None,
            },
        )
        if failed:
            self._emit(
                "retry",
                agent,
                ok=False,
                error_type="model_call_retry",
                payload_summary=f"retry after {failed} failed model call(s)",
                extra={
                    "attempt": failed + 1,
                    "attempt_kind": "model_call",
                    "inferred": True,
                    "inferred_from": "error then model_call for the same agent",
                },
            )
        return None

    async def on_model_error_callback(
        self, callback_context: CallbackContext, llm_request, error: Exception
    ):
        agent = getattr(callback_context, "agent_name", "supervisor")
        self._model_failures[agent] = self._model_failures.get(agent, 0) + 1
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
    @staticmethod
    def _tool_key(tool: BaseTool, tool_context, tool_args: dict) -> str:
        """One key per invocation; tool name alone collides on parallel calls."""
        call_id = getattr(tool_context, "function_call_id", None)
        if call_id:
            return f"{tool.name}:{call_id}"
        # Unit-test callbacks omit the ADK context id. Keep the old behaviour
        # there rather than creating an unmatched per-argument key.
        return f"{tool.name}:callback"

    @staticmethod
    def _stored_args(tool: BaseTool, tool_args: dict) -> dict:
        """Record the URL the tool actually receives, not a Markdown link."""
        stored = dict(tool_args or {})
        if tool.name == "fetch_page":
            stored["url"] = plain_http_url(stored.get("url"))
        return stored

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context
    ) -> Optional[dict]:
        # Only the start time and normalized arguments are recorded here; the
        # event itself is emitted in after_tool_callback, where the outcome is
        # known. The key must include function_call_id because ADK can run
        # several calls to the same tool concurrently.
        key = self._tool_key(tool, tool_context, tool_args)
        self._starts[key] = time.perf_counter()
        self._args[key] = _summary(self._stored_args(tool, tool_args), limit=240)
        return None

    async def after_tool_callback(
        self, *, tool: BaseTool, tool_args: dict, tool_context, result: dict
    ) -> Optional[dict]:
        key = self._tool_key(tool, tool_context, tool_args)
        started = self._starts.pop(key, None)
        if started is None:
            # Defensive fallback for paths where the ID was unavailable in
            # before_tool. Do not let a stale key suppress a real outcome.
            for candidate in list(self._starts):
                if candidate.startswith(f"{tool.name}:"):
                    started = self._starts.pop(candidate)
                    key = candidate
                    break
        error = None
        error_type = "ToolError"
        blocked_by = None
        structured = None
        if isinstance(result, dict):
            if result.get("isError") or result.get("error"):
                error = str(result.get("error") or "tool returned isError")
                error_type = str(result.get("error_type") or "ToolError")
                blocked_by = result.get("blocked_by")
            else:
                # Athena's tools report a failure as a normal typed result with
                # ok=false (a blocked fetch is not an MCP transport error).
                structured = result.get("structuredContent")
                if isinstance(structured, dict) and structured.get("ok") is False:
                    error = str(
                        structured.get("error")
                        or structured.get("status")
                        or "tool returned ok=false"
                    )
        sources = _source_rows(tool.name, result)
        fields = {
            "duration_ms": (
                round((time.perf_counter() - started) * 1000, 2)
                if started is not None else None
            ),
            "ok": error is None,
            "error_type": error_type if error else None,
            "error_message": error,
            "payload_summary": f"{tool.name}: {self._args.pop(key, '')}",
        }
        extra: dict[str, Any] = {
            "function_call_id": getattr(tool_context, "function_call_id", None),
            "tool": tool.name,
            "agent": getattr(tool_context, "agent_name", "researcher"),
        }
        if blocked_by:
            extra["blocked_by"] = str(blocked_by)
        if isinstance(structured, dict):
            result_status = structured.get("status")
            if result_status:
                extra["result_status"] = str(result_status)
            if tool.name == "fetch_page":
                extra["requested_url"] = structured.get("requested_url") or ""
                extra["final_url"] = structured.get("final_url") or ""
            if tool.name == "web_search":
                extra["provider"] = structured.get("provider") or ""
        if sources:
            extra["sources"] = sources
        fields["extra"] = extra
        agent_name = getattr(tool_context, "agent_name", "researcher")
        failure_key = (agent_name, tool.name)
        failed_before = self._tool_failures.get(failure_key, 0)
        if error is None:
            self._tool_failures.pop(failure_key, None)
        else:
            self._tool_failures[failure_key] = failed_before + 1
        self._emit("tool_call", agent_name, **fields)
        if error is None and failed_before:
            # Same agent, same tool, and the previous attempt(s) failed: this is
            # a retry attempt caught by event order, not by a callback.
            self._emit(
                "retry",
                agent_name,
                ok=True,
                payload_summary=f"retry {tool.name} after {failed_before} failed attempt(s)",
                extra={
                    "attempt": failed_before + 1,
                    "attempt_kind": "tool_call",
                    "tool": tool.name,
                    "inferred": True,
                    "inferred_from": "tool_call ok after failed tool_call for the same tool",
                },
            )
        return None
