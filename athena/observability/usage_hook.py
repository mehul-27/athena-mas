"""The litellm CustomLogger that feeds the process-wide usage totals.

Kept apart from ``litellm_usage`` so the accounting (totals, scopes, settle) and
the framework hook can each stay small. CrewAI resets ``litellm.callbacks`` when
it builds an LLM (``crewai/llm.py:707``), so installing is not a one-time act:
call ``install_usage_logger()`` again right before a role runs.
"""

from __future__ import annotations

import threading

from athena.observability.context import current_agent
from athena.observability.litellm_usage import TOTALS, usage_from_response
from athena.observability.recorder import get_recorder

_LOGGER = None


def record(response_obj) -> None:
    """Count one completion's usage. Never raises."""
    try:
        tokens_in, tokens_out, reasoning = usage_from_response(response_obj)
    except Exception:  # noqa: BLE001 - monitoring must never break a call
        return
    TOTALS.add(tokens_in, tokens_out, reasoning)
    note_over_cap(tokens_out)


def note_over_cap(completion_tokens: int | None) -> None:
    """Record a ``guardrail`` event when a completion blows past the role cap.

    Passive: it emits an event and changes nothing. The clamp itself stays in
    ``athena/config/call_limits.py``; this is the observable half of it, and it
    is the only guardrail emit site reachable from the usage path.
    """
    try:
        from athena.config import call_limits
        from athena.observability.hooks import framework_for

        role, cap = call_limits.current()
        if not cap or not completion_tokens:
            return
        if int(completion_tokens) <= call_limits.RUNAWAY_FACTOR * cap:
            return
        recorder = get_recorder()
        if recorder is None:
            return
        recorder.emit(
            "guardrail",
            role or current_agent(),
            framework_for(role),
            ok=True,
            payload_summary=(
                f"guardrail runaway_output: flagged {int(completion_tokens)} "
                f"completion tokens against a cap of {cap}"
            ),
            extra={
                "rule": "runaway_output",
                "action": "flag",
                "stage": "model_call",
                "role": role,
                "cap": int(cap),
                "factor": call_limits.RUNAWAY_FACTOR,
                "completion_tokens": int(completion_tokens),
            },
        )
    except Exception:  # noqa: BLE001 - monitoring must never break a call
        return


_SEEN: set[str] = set()
_SEEN_LOCK = threading.Lock()
SEEN_LIMIT = 500


def record_once(response_obj) -> None:
    """Count a response once, even if two layers see it (dedupe by id)."""
    response_id = None
    if isinstance(response_obj, dict):
        response_id = response_obj.get("id")
    else:
        response_id = getattr(response_obj, "id", None)
    if response_id:
        with _SEEN_LOCK:
            if response_id in _SEEN:
                return
            if len(_SEEN) >= SEEN_LIMIT:
                _SEEN.clear()
            _SEEN.add(response_id)
    record(response_obj)


def build_logger():
    from litellm.integrations.custom_logger import CustomLogger

    class UsageLogger(CustomLogger):
        def log_success_event(self, kwargs, response_obj, start_time, end_time):
            record(response_obj)

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
            record(response_obj)

    return UsageLogger()


def install_usage_logger() -> bool:
    """Re-assert our logger, surviving CrewAI's callback reset. Idempotent."""
    global _LOGGER
    import litellm

    if _LOGGER is None:
        _LOGGER = build_logger()

    callbacks = list(getattr(litellm, "callbacks", None) or [])
    if any(callback is _LOGGER for callback in callbacks):
        return False
    litellm.callbacks = callbacks + [_LOGGER]
    return True


def callback_count() -> int:
    import litellm

    return len(getattr(litellm, "callbacks", None) or [])
