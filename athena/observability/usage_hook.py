"""The litellm CustomLogger that feeds the process-wide usage totals.

Kept apart from ``litellm_usage`` so the accounting (totals, scopes, settle) and
the framework hook can each stay small. CrewAI resets ``litellm.callbacks`` when
it builds an LLM (``crewai/llm.py:707``), so installing is not a one-time act:
call ``install_usage_logger()`` again right before a role runs.
"""

from __future__ import annotations

from athena.observability.litellm_usage import TOTALS, usage_from_response

_LOGGER = None


def record(response_obj) -> None:
    """Count one completion's usage. Never raises."""
    try:
        tokens_in, tokens_out, reasoning = usage_from_response(response_obj)
    except Exception:  # noqa: BLE001 - monitoring must never break a call
        return
    TOTALS.add(tokens_in, tokens_out, reasoning)


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
