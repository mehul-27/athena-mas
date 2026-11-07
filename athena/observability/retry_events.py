"""A ``retry`` event: one attempt of the same work, tried again.

Every real retry in this project reports the same shape, so the telemetry view
can count explicit retries without guessing and without conflating them with
inferred ones (an ``error`` event whose type the pipeline is known to retry, or a
``retry`` event ADK's plugin deduced from event order — both carry
``extra.inferred = True``).

``extra`` on a retry event
--------------------------
``attempt``       which attempt this is, 1-based (a first retry is ``2``)
``of``            the attempt cap when the caller knows it (``None`` otherwise)
``retry_type``    why the attempt is being repeated: ``empty_reply``,
                  ``runaway_output``, ``truncated_report``, ``invalid_json``,
                  ``model_call``, ``tool_call``, ``role_failure``, ``other``
``reason``        one redacted sentence (never the payload)
``error_type``    the machine-readable reason, mirroring the matching ``error`` event
``stage``         the pipeline stage that retried
``token_budget``  ``{"requested": n, "retried": m}`` when the retry changed the
                  output budget — the whole point of the empty-reply retry
``inferred``      ``True`` only when no code path reported the retry
``inferred_from`` how an inference was drawn (event order, error type)
``framework``     redundant with ``Event.framework``; kept for the UI's detail row

``Event.ts`` is the timestamp and ``Event.run_id``/``agent``/``framework`` carry
the run, the role and the framework, so the caller only supplies the fields it
actually knows.

Rule: **never emit a retry that did not happen.** These helpers are called from
the branch that has just decided to re-run something, never from a branch that
merely suspects it might. The tests assert both directions.
"""

from __future__ import annotations

from typing import Any

from athena.observability.recorder import Recorder, get_recorder

RETRY_TYPES: tuple[str, ...] = (
    "empty_reply",
    "runaway_output",
    "truncated_report",
    "invalid_json",
    "model_call",
    "tool_call",
    "role_failure",
    "other",
)

MAX_REASON_CHARS = 200


def _clean_reason(value: Any) -> str:
    """Redact then truncate one reason sentence. Never raises."""
    text = str(value if value is not None else "")
    try:
        from athena.guardrails.secrets import redact_text

        text = redact_text(text) or ""
    except Exception:  # noqa: BLE001 - redaction must never break an emission
        pass
    return " ".join(text.split())[:MAX_REASON_CHARS]


def _framework_of(agent: str, framework: str | None) -> str:
    if framework:
        return framework
    try:
        from athena.observability.hooks import framework_for

        return framework_for(agent)
    except Exception:  # noqa: BLE001 - a label is not worth a failure
        return "adk"


def _target(recorder: Recorder | None) -> Recorder | None:
    return recorder if recorder is not None else get_recorder()


def note_retry(
    agent: str | None,
    reason: str,
    retry_type: str = "other",
    framework: str | None = None,
    *,
    attempt: int | None = None,
    of: int | None = None,
    stage: str = "",
    error_type: str | None = None,
    token_budget: dict[str, Any] | None = None,
    inferred: bool = False,
    inferred_from: str = "",
    ok: bool = False,
    recorder: Recorder | None = None,
    **extra: Any,
) -> bool:
    """Record one retry. Returns whether an event was written.

    ``attempt`` is 1-based: the first retry after an initial attempt is
    ``attempt=2, of=2``. Pass ``token_budget`` when the retry changed the output
    budget, which is what the empty-reply retry does.
    """
    try:
        target = _target(recorder)
        if target is None:
            return False
        who = agent or "supervisor"
        payload: dict[str, Any] = {
            "retry_type": str(retry_type or "other"),
            # Kept for readers written against the earlier ``attempt_kind``
            # field; ``retry_type`` is the name the telemetry docs use.
            "attempt_kind": str(retry_type or "other"),
            "reason": _clean_reason(reason),
            "framework": str(framework or "adk"),
        }
        if attempt is not None:
            payload["attempt"] = int(attempt)
        if of is not None:
            payload["of"] = int(of)
        if stage:
            payload["stage"] = str(stage)[:40]
        if error_type:
            payload["error_type"] = str(error_type)[:80]
        if token_budget:
            budget = {
                key: value
                for key, value in dict(token_budget).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
            if budget:
                payload["token_budget"] = budget
        if inferred:
            payload["inferred"] = True
            if inferred_from:
                payload["inferred_from"] = str(inferred_from)[:120]
        payload.update(extra)
        target.emit(
            "retry",
            who,
            framework or _framework_of(who, framework),
            ok=ok,
            error_type=error_type or str(retry_type or "") or None,
            payload_summary=(
                f"retry {retry_type} (attempt {attempt})"
                if attempt is not None else f"retry {retry_type}"
            )[:300],
            extra=payload,
        )
        return True
    except Exception:  # noqa: BLE001 - monitoring must never break a run
        return False


def retry_empty_reply(
    framework: str,
    budget: int | None,
    retried_budget: int | None,
    *,
    agent: str | None = None,
    reason: str = "empty reply: no visible text (finish_reason=length); retrying with a larger budget",
    attempt: int = 2,
    of: int = 2,
    recorder: Recorder | None = None,
) -> bool:
    """The empty-reply retry, as one ``retry`` event.

    This is the call the empty-reply path makes *instead of* only emitting the
    ``error`` event: the error says the attempt failed, this says the failure
    was retried and with which budget.
    """
    budget_out: dict[str, Any] = {}
    if budget is not None:
        budget_out["requested"] = int(budget)
    if retried_budget is not None:
        budget_out["retried"] = int(retried_budget)
    return note_retry(
        agent,
        reason,
        "empty_reply",
        framework,
        attempt=attempt,
        of=of,
        stage="model_call",
        error_type="empty_reply",
        token_budget=budget_out or None,
        recorder=recorder,
    )


def retry_truncated_report(
    reason: str = "the generated answer was truncated",
    *,
    agent: str = "writer",
    framework: str = "crewai",
    attempt: int = 2,
    recorder: Recorder | None = None,
) -> bool:
    """The writer's "cut-off answer, ask again" retry."""
    return note_retry(
        agent,
        reason,
        "truncated_report",
        framework,
        attempt=attempt,
        of=2,
        stage="writer",
        error_type="truncated_report",
        recorder=recorder,
    )


def retry_invalid_output(
    error: BaseException | str,
    *,
    agent: str,
    framework: str | None = None,
    attempt: int = 2,
    of: int = 2,
    recorder: Recorder | None = None,
) -> bool:
    """A rejected/reparse retry: the previous output was unusable."""
    detail = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    return note_retry(
        agent,
        f"previous output was rejected: {detail}",
        "invalid_json",
        framework,
        attempt=attempt,
        of=of,
        stage="generate",
        error_type="invalid_output",
        recorder=recorder,
    )
