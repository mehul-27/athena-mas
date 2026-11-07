"""Provider-side tool-call rejection: one bounded, observable retry.

The model sometimes emits a tool call the provider refuses before it ever
reaches us::

    BadRequestError: GroqException - {"error":{"message":"tool call validation
    failed: attempted to call tool 'webollar' which was not in request.tools",
    "type":"invalid_request_error","code":"tool_use_failed"}}

The corrupted name (``webollar``) is not a tool anyone offered. The same class
also covers a call whose arguments do not match the published schema. Neither
is a *pipeline* failure: three good evidence items were already on the
blackboard when this killed the pilot run, and the analyst/writer/critic stages
never got to run.

What this module owns:

* :func:`is_tool_call_validation_error` — recognise exactly that class of
  provider error, and nothing else (a context-length or auth error is not
  retried here and keeps propagating);
* :func:`offending_tool` — the name the model actually emitted, read out of the
  provider message for the record only. It is never used as a tool name;
* :func:`permitted_tools` / :func:`corrective_message` — the corrective
  instruction that lists the **exact** tool names and argument schemas the
  request offered, so the retry cannot invent one;
* :func:`degraded_response` — what the agent sees when even the retry was
  rejected: an instruction to stop calling tools and let the pipeline continue
  with the evidence already recorded;
* :func:`degradations` — read the recorded rejections back, so a degraded run is
  reported as degraded rather than as a clean success.

Rules, mirroring ``observability/retry_events.py``: **never emit a retry that did
not happen** (the caller emits only after deciding to retry), at most **one**
retry per model call, and **never invent** a tool name, argument or URL.
"""

from __future__ import annotations

import re
from typing import Any

# Phrases that identify a provider-side tool-call validation failure. Every one
# of them names a tool, so a non-tool 400 cannot match by accident.
REJECTION_MARKERS: tuple[str, ...] = (
    "tool call validation failed",
    "tool_use_failed",
    "was not in request.tools",
    "is not in request.tools",
    "did not match schema",
    "tool call is not valid",
    "failed_generation",
)

_QUOTED_TOOL = re.compile(r"""tool\s+['"`]([^'"`]+)['"`]""", re.IGNORECASE)
_UNQUOTED_TOOL = re.compile(
    r"""tool\s+([A-Za-z0-9_.\-]+)\s+(?:which|is not|was not|did not)""",
    re.IGNORECASE,
)
MAX_TOOLS_LISTED = 24
EVENT_MARKER = "tool_call_rejected"


def is_tool_call_validation_error(error: BaseException | str) -> bool:
    """True only for a provider rejection *of a tool call*.

    Deliberately narrow: the message must mention a tool **and** one of the
    marker phrases. A context-length, quota or auth 400 is not this class and
    must keep propagating to the caller unchanged.
    """
    text = str(error or "").lower()
    if "tool" not in text:
        return False
    return any(marker in text for marker in REJECTION_MARKERS)


def offending_tool(error: BaseException | str) -> str:
    """The tool name the model emitted, for the record only. Never executed.

    Only a name the provider actually quoted or named after ``tool`` is
    returned, so prose such as "tool call validation failed" cannot be read as
    a tool called ``call`` and then repeated into a log.
    """
    text = str(error or "")
    for pattern in (_QUOTED_TOOL, _UNQUOTED_TOOL):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def _type_name(schema: Any) -> str:
    """The JSON-ish type of one argument, as the declaration states it."""
    kind = getattr(schema, "type", None)
    value = getattr(kind, "value", None) or kind
    text = str(value or "").lower()
    return text or "any"


def permitted_tools(llm_request: Any) -> list[tuple[str, str]]:
    """``(name, argument summary)`` for every tool this request actually offers.

    Read from the request the model is about to see, not from the registry, so
    the corrective instruction cannot list a tool this agent was never given.
    """
    rows: list[tuple[str, str]] = []
    config = getattr(llm_request, "config", None)
    for tool in getattr(config, "tools", None) or []:
        for declaration in getattr(tool, "function_declarations", None) or []:
            name = str(getattr(declaration, "name", "") or "")
            if not name:
                continue
            parameters = getattr(declaration, "parameters", None)
            properties = getattr(parameters, "properties", None) or {}
            required = [str(item) for item in getattr(parameters, "required", None) or []]
            parts: list[str] = []
            for argument, schema in properties.items():
                kind = _type_name(schema)
                label = "required" if argument in required else "optional"
                parts.append(f"{argument}: {kind} ({label})")
            rows.append((name, ", ".join(parts) or "no arguments"))
            if len(rows) >= MAX_TOOLS_LISTED:
                return rows
    return rows


def corrective_message(
    error: BaseException | str, llm_request: Any
) -> str:
    """The instruction the retried model call carries. Never invents a tool."""
    rejected = offending_tool(error)
    rows = permitted_tools(llm_request)
    lines = [
        "TOOL CALL REJECTED BY THE PROVIDER.",
        (
            f"Your previous reply attempted to call `{rejected}`, which is not a "
            "tool offered in this request."
            if rejected
            else "Your previous tool call did not match the published schema."
        ),
        "That call was not executed and recorded no evidence.",
    ]
    if rows:
        lines.append("The only permitted tools and their arguments are:")
        lines.extend(f"- `{name}`({arguments})" for name, arguments in rows)
        lines.append(
            "Call one of these exact names with every required argument present, "
            "or reply with the single word DONE. Do not invent a tool name, and "
            "do not invent a URL."
        )
    else:
        lines.append(
            "No tools are offered in this request. Reply with the single word DONE."
        )
    return "\n".join(lines)


def with_corrective_message(llm_request: Any, text: str) -> Any:
    """A copy of the request with the corrective turn appended.

    The original request object belongs to ADK and is reused across the agent's
    turns, so it is copied rather than mutated: the correction must not leak
    into the next model call.
    """
    from google.genai import types

    correction = types.Content(role="user", parts=[types.Part(text=text)])
    try:
        retry_request = llm_request.model_copy()
    except AttributeError:  # a duck-typed request in a test
        retry_request = llm_request
    contents = list(getattr(llm_request, "contents", []) or [])
    contents.append(correction)
    try:
        retry_request.contents = contents
    except (AttributeError, ValueError):  # pragma: no cover - read-only stub
        pass
    return retry_request


DEGRADED_TEXT = (
    "STOP CALLING TOOLS. The provider rejected the tool call twice, so tool use "
    "is disabled for the rest of this stage. Do not retry the call. Reply with "
    "the single word DONE and let the pipeline continue with the evidence "
    "already recorded."
)


def degraded_response(error: BaseException | str) -> Any:
    """The response the agent sees when the one allowed retry also failed.

    A plain, non-error text response: the stage finishes normally, so the
    writer still runs and the run is reported as degraded rather than lost.
    """
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    return LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part(text=DEGRADED_TEXT)]
        ),
        custom_metadata={
            "athena_degraded": EVENT_MARKER,
            "reason": str(error or "")[:200],
        },
    )


def _recorder():
    from athena.observability.recorder import get_recorder

    return get_recorder()


def record_rejection(
    error: BaseException | str,
    agent: str,
    *,
    will_retry: bool,
    attempt: int = 1,
    of: int = 2,
) -> None:
    """Emit one ``error`` event for a rejected tool call. Never raises."""
    recorder = _recorder()
    if recorder is None:
        return
    rejected = offending_tool(error)
    message = str(error or "")
    try:
        recorder.emit(
            "error",
            agent,
            "adk",
            ok=False,
            error_type="ToolCallRejected",
            error_message=message[:300],
            payload_summary=(
                f"tool call rejected by provider"
                + (f": {rejected}" if rejected else "")
                + ("; retrying once" if will_retry else "; tool use disabled")
            )[:300],
            extra={
                "event": EVENT_MARKER,
                "rejected_tool": rejected,
                "attempt": attempt,
                "of": of,
                "will_retry": will_retry,
            },
        )
    except Exception:  # noqa: BLE001 - monitoring must never break a call
        pass


def record_degraded(error: BaseException | str, agent: str) -> None:
    """Emit the one event that marks the run degraded. Never raises."""
    recorder = _recorder()
    if recorder is None:
        return
    try:
        recorder.emit(
            "error",
            agent,
            "adk",
            ok=False,
            error_type="ToolCallDegraded",
            error_message=str(error or "")[:300],
            payload_summary="tool use disabled for this stage after 2 rejections",
            extra={"event": EVENT_MARKER, "degraded": True},
        )
    except Exception:  # noqa: BLE001 - monitoring must never break a call
        pass


def degradations(run_id: str, store: Any = None) -> list[dict[str, Any]]:
    """The tool-call rejections an ADK run recorded, in event order."""
    rows: list[dict[str, Any]] = []
    try:
        from athena.observability.store import SQLiteEventStore

        events = SQLiteEventStore.from_env().events(run_id)
    except Exception:  # noqa: BLE001 - a status view must never break a run
        return rows
    for event in events:
        extra = getattr(event, "extra", None) or {}
        if extra.get("event") != EVENT_MARKER:
            continue
        rows.append(
            {
                "agent": str(getattr(event, "agent", "") or ""),
                "error_type": str(getattr(event, "error_type", "") or ""),
                "rejected_tool": str(extra.get("rejected_tool") or ""),
                "degraded": bool(extra.get("degraded")),
                "attempt": extra.get("attempt"),
            }
        )
    return rows


def describe(rows: list[dict[str, Any]]) -> list[str]:
    """One short line per rejection, for the summary's stage errors."""
    lines: list[str] = []
    for row in rows:
        who = row.get("agent") or "model"
        tool = row.get("rejected_tool") or ""
        if row.get("degraded"):
            lines.append(
                f"{who}: tool call rejected by provider twice; "
                f"tool use disabled, run degraded"
            )
        elif tool:
            lines.append(f"{who}: provider rejected tool call to {tool!r}; retried once")
        else:
            lines.append(f"{who}: provider rejected a tool call; retried once")
    return list(dict.fromkeys(lines))


def note_on_summary(summary: Any, run_id: str, store: Any = None) -> list[dict[str, Any]]:
    """Fold this run's rejections into its summary, so it is never a clean success.

    A run whose model could not call a tool is not a clean run: it is reported
    as degraded with the reason in ``stage_errors`` and ``degraded_reason``, and
    that is distinguishable from a run that never had a rejection at all.
    """
    rows = degradations(run_id, store)
    if not rows:
        return []
    from athena.summary import mark_degraded

    lines = describe(rows)
    for line in lines:
        if line not in summary.stage_errors:
            summary.stage_errors.append(line)
    mark_degraded(
        summary,
        "the provider rejected a tool call: " + (lines[0] if lines else "tool use limited"),
    )
    return rows


__all__ = [
    "DEGRADED_TEXT",
    "EVENT_MARKER",
    "REJECTION_MARKERS",
    "corrective_message",
    "degradations",
    "degraded_response",
    "describe",
    "is_tool_call_validation_error",
    "note_on_summary",
    "offending_tool",
    "permitted_tools",
    "record_degraded",
    "record_rejection",
    "with_corrective_message",
]
