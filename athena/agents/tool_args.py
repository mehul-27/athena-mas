"""Required-argument validation and bounded correction for tool calls.

Observed failure: a provider rejected the whole request with

    tool call validation failed: parameters for tool fetch_page did not match
    schema: missing properties: 'url'

because the model emitted a ``fetch_page`` call with no ``url``. The published
schema *does* require ``url`` — the call was simply incomplete — so the fix is
not a looser schema. It is:

* keep the required-argument list authoritative and check it against the tool's
  own published schema before the call is attempted;
* answer a malformed call with an explicit validation error the model can read,
  instead of letting it reach the tool server and fail as a transport error;
* allow exactly one correction attempt per tool per worker, then refuse further
  attempts outright — never an unbounded retry loop;
* never invent a missing argument. A URL is the model's job to supply; an
  absent one stays absent.

A malformed call records **no evidence**. Nothing here fabricates a source.
"""

from __future__ import annotations

from typing import Any

from athena.observability.recorder import get_recorder

# Fallback for a tool whose schema cannot be read (a stub in a test, or a
# future server). These mirror the MCP servers' published ``required`` lists.
FALLBACK_REQUIRED: dict[str, tuple[str, ...]] = {
    "rag_search": ("query",),
    "web_search": ("query",),
    "fetch_page": ("url",),
    "calculate": ("expression",),
    "forecast": ("values", "horizon"),
}

# Arguments that are meaningless when empty or whitespace-only.
TEXT_ARGS = frozenset({"query", "url", "expression"})

MAX_CORRECTIONS = 1


def required_args(tool: Any) -> list[str]:
    """The tool's own published ``required`` list, else the fallback map."""
    schema = getattr(getattr(tool, "_mcp_tool", None), "inputSchema", None)
    if isinstance(schema, dict):
        declared = schema.get("required")
        if isinstance(declared, list) and declared:
            return [str(name) for name in declared]
    name = getattr(tool, "name", "") or ""
    return list(FALLBACK_REQUIRED.get(name, ()))


def missing_args(tool: Any, args: Any) -> list[str]:
    """Required arguments that are absent, or empty where emptiness is invalid."""
    if not isinstance(args, dict):
        return required_args(tool)
    missing: list[str] = []
    for name in required_args(tool):
        if name not in args or args[name] is None:
            missing.append(name)
            continue
        if name in TEXT_ARGS and not str(args[name]).strip():
            missing.append(name)
    return missing


def _record(tool_name: str, missing: list[str], agent: str, attempt: int) -> None:
    """Record the malformed call. Never raises."""
    recorder = get_recorder()
    if recorder is None:
        return
    recorder.emit(
        "error",
        agent or "researcher",
        "adk",
        ok=False,
        error_type="MalformedToolCall",
        error_message=(
            f"{tool_name}: missing required argument(s) "
            f"{', '.join(missing)}; attempt {attempt} of {MAX_CORRECTIONS + 1}"
        ),
        payload_summary=f"malformed {tool_name} call: missing {', '.join(missing)}",
        extra={
            "tool": tool_name,
            "missing": list(missing),
            "attempt": attempt,
            "correction_allowed": attempt <= MAX_CORRECTIONS,
        },
    )


class MalformedCallGuard:
    """Refuse an incomplete tool call, allowing one bounded correction.

    One instance per worker, so the allowance is per worker rather than global:
    a second worker's first mistake is still correctable.
    """

    def __init__(self, agent: str = "researcher") -> None:
        self.agent = agent
        self.attempts: dict[str, int] = {}
        self.rejected = 0

    def check(self, tool: Any, args: Any) -> dict | None:
        """``None`` to let the call through, else the error the model receives."""
        name = getattr(tool, "name", "") or "tool"
        missing = missing_args(tool, args)
        if not missing:
            return None
        attempt = self.attempts.get(name, 0) + 1
        self.attempts[name] = attempt
        self.rejected += 1
        _record(name, missing, self.agent, attempt)
        allowed = attempt <= MAX_CORRECTIONS
        if allowed:
            guidance = (
                f"Re-issue the call once with every required argument present: "
                f"{', '.join(required_args(tool))}. Do not invent a value you do "
                f"not have; if you cannot supply {missing[0]}, do not call "
                f"`{name}` again."
            )
        else:
            guidance = (
                f"No further `{name}` attempts will be accepted. Record the gap "
                f"and reply DONE."
            )
        return {
            "isError": True,
            "error": f"Invalid call to `{name}`: missing required argument(s) "
                     f"{', '.join(missing)}. {guidance}",
            "error_type": "ToolArgsInvalid",
            "blocked_by": "malformed_args",
            "tool": name,
            "missing": list(missing),
            "retry_allowed": allowed,
            "recorded": 0,
        }


__all__ = [
    "MalformedCallGuard",
    "FALLBACK_REQUIRED",
    "MAX_CORRECTIONS",
    "missing_args",
    "required_args",
]
