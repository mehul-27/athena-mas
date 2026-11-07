"""Process-wide hooks that turn library activity into events.

Installed once per process (runner and CrewAI server). Each hook looks up the
active recorder at call time, so a per-run recorder is honoured without passing
it through the libraries.

The ``emit_*`` helpers at the bottom are the one-line API for the pipeline's own
retry / guardrail / handoff sites: they resolve the framework from the agent name,
look up the recorder, and never raise (``Recorder.emit`` already swallows a store
failure). A module that knows it retried something calls ``emit_retry`` instead of
building an ``Event`` by hand, so every retry is recorded the same way.
"""

from __future__ import annotations

from athena.blackboard import set_write_listener
from athena.config import ratelimit
from athena.observability.recorder import get_recorder

CREWAI_AGENTS = {"analyst", "writer", "critic"}


def framework_for(agent: str) -> str:
    return "crewai" if agent in CREWAI_AGENTS else "adk"


def _on_wait(waited_ms: float, tokens: int, agent: str) -> None:
    recorder = get_recorder()
    if recorder is None:
        return
    recorder.emit(
        "limiter_wait",
        agent or "supervisor",
        framework_for(agent),
        duration_ms=waited_ms,
        extra={"tokens_requested": tokens},
    )


def _on_write(section: str, version: int, author: str, operation: str) -> None:
    recorder = get_recorder()
    if recorder is None:
        return
    recorder.emit(
        "blackboard_write",
        author,
        framework_for(author),
        payload_summary=f"{operation} {section} v{version}",
        extra={"section": section, "version": version, "operation": operation},
    )


def install_hooks() -> None:
    """Route limiter waits and blackboard writes into the active recorder."""
    ratelimit.set_wait_listener(_on_wait)
    set_write_listener(_on_write)


def remove_hooks() -> None:
    ratelimit.set_wait_listener(None)
    set_write_listener(None)


# --- additive emit sites for retry / guardrail / handoff --------------------
def emit_retry(
    agent: str,
    reason: str,
    framework: str | None = None,
    *,
    attempt: int | None = None,
    of: int | None = None,
    attempt_kind: str = "",
    error_type: str | None = None,
    inferred: bool = False,
    **extra,
) -> None:
    """Record one retry: the same work is being attempted again.

    ``inferred`` is set when no code path reported a retry and the attempt was
    deduced from event order; it keeps a deduction distinguishable from a fact.

    Thin wrapper over :func:`athena.observability.retry_events.note_retry` so
    every retry site writes the same ``extra`` shape (``retry_type``,
    ``attempt``, ``of``, ``reason``, ``token_budget`` …). ``attempt_kind`` is
    carried as ``retry_type`` when no explicit type is given.
    """
    from athena.observability.retry_events import note_retry

    note_retry(
        agent,
        reason,
        (extra.pop("retry_type", None) or attempt_kind or "other"),
        framework,
        attempt=attempt,
        of=of,
        error_type=error_type,
        inferred=inferred,
        **extra,
    )


def emit_guardrail(
    agent: str,
    rule: str,
    action: str,
    detail: str = "",
    framework: str | None = None,
    *,
    stage: str = "",
    **extra,
) -> None:
    """Record one guardrail decision (a clamp, a block, a repair).

    Thin wrapper over :func:`athena.observability.guardrail_events.record_decision`,
    which redacts and truncates ``detail`` and never stores the payload.
    """
    from athena.observability.guardrail_events import record_decision

    record_decision(
        stage=stage or "guardrail",
        rule=rule,
        action=action,
        detail=detail,
        screen=extra.pop("screen", "emit_guardrail"),
        agent=agent or "supervisor",
        framework=framework,
    )


def emit_handoff(
    agent: str,
    framework: str | None = None,
    *,
    to_agent: str = "",
    via: str = "",
    role: str = "",
    **extra,
) -> None:
    """Record a handoff: control moved to another agent or framework."""
    recorder = get_recorder()
    if recorder is None:
        return
    payload = {"to": to_agent or role or "", **extra}
    if via:
        payload["via"] = via
    if role:
        payload["role"] = role
    recorder.emit(
        "handoff",
        agent or "supervisor",
        framework or framework_for(agent),
        payload_summary=f"handoff to {to_agent or role or 'next agent'}"
        f"{' via ' + via if via else ''}"[:300],
        extra=payload,
    )
