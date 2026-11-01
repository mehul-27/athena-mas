"""Runtime guard: every LLM call must go to the configured endpoint.

When ``LLM_API_BASE`` is set, a call that resolves to a *different* host (or to
no host at all, i.e. the SDK default) is a configuration bug — and a dangerous
one, because it ships the gateway's key to the wrong service. This module wraps
the completion entry points and raises ``EndpointMismatchError`` instead.

Messages contain hostnames only, never keys or full URLs.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

ENV_GUARD = "ATHENA_ENDPOINT_GUARD"

_INSTALLED = False
_LAST_HOST: str = ""

# A chat turn may target a provider profile whose base URL differs from the
# process's LLM_API_BASE. The scope lets that one call state its expected host
# without mutating the environment. `""` means "no host to verify" (the provider
# default), which disables the check for that call only.
_SCOPE: ContextVar[str | None] = ContextVar("athena_expected_host", default=None)


class EndpointMismatchError(RuntimeError):
    """A model call would have gone to an unexpected host."""


def host_of(url: str) -> str:
    """Hostname of a URL (no scheme, no path, no query)."""
    if not url:
        return ""
    parsed = urlparse(url if "//" in url else f"//{url}")
    return (parsed.hostname or "").lower()


def expected_host() -> str:
    scoped = _SCOPE.get()
    if scoped is not None:
        return scoped
    return host_of(os.getenv("LLM_API_BASE") or "")


@contextmanager
def expected_host_scope(base_url: str | None):
    """Verify calls in this block against ``base_url`` (``None`` = no check)."""
    token = _SCOPE.set(host_of(base_url or ""))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def guard_active() -> bool:
    if (os.getenv(ENV_GUARD) or "").strip() == "0":
        return False
    return bool(expected_host())


def provider_base_env(model: str) -> str:
    """The base URL the provider SDK would fall back to, if any."""
    prefix = (model or "").split("/", 1)[0].lower() or "openai"
    for name in (f"{prefix.upper()}_API_BASE", f"{prefix.upper()}_BASE_URL"):
        value = os.getenv(name)
        if value:
            return value
    return ""


def check_call(model: str, kwargs: dict) -> None:
    """Raise when this call would not reach LLM_API_BASE's host."""
    global _LAST_HOST
    if not guard_active():
        return

    base = (
        kwargs.get("api_base")
        or kwargs.get("base_url")
        or provider_base_env(model)
    )
    resolved = host_of(base)
    _LAST_HOST = resolved or "(default)"
    wanted = expected_host()

    if resolved != wanted:
        shown = resolved or "the provider default (no api_base)"
        raise EndpointMismatchError(
            f"model call for '{model}' would go to {shown}, "
            f"but LLM_API_BASE points at {wanted}"
        )


def last_host() -> str:
    """Host of the most recent guarded call, for the preflight report."""
    return _LAST_HOST


def install_guard() -> bool:
    """Wrap the completion entry points. Idempotent; no-op when disabled.

    Arming is independent of whether LLM_API_BASE is set: a chat turn can carry
    its own expected host in ``expected_host_scope``. When no host is expected,
    ``check_call`` is a no-op, so the previous behaviour is preserved.
    """
    global _INSTALLED
    if (os.getenv(ENV_GUARD) or "").strip() == "0":
        return False
    # Not "install once": tests and scripts may replace litellm's entry points,
    # so every call checks the *current* function instead of trusting a flag.

    import litellm

    def capture(result):
        """Count usage and flag a runaway completion."""
        try:
            from athena.observability.litellm_usage import usage_from_response
            from athena.observability.usage_hook import record_once
            from athena.config.call_limits import note_completion

            record_once(result)
            _in, out, _reasoning = usage_from_response(result)
            note_completion(out)
        except Exception:  # noqa: BLE001 - monitoring must never break a call
            pass

    def _prepare(kwargs: dict) -> None:
        """Clamp the call to the running role's cap and timeout."""
        from athena.config.call_limits import clamp

        clamp(kwargs)

    def wrap(function):
        def guarded(*args, **kwargs):
            model = kwargs.get("model") or (args[0] if args else "")
            check_call(str(model), kwargs)
            _prepare(kwargs)
            result = function(*args, **kwargs)
            capture(result)
            return result

        return guarded

    def awrap(function):
        async def guarded(*args, **kwargs):
            model = kwargs.get("model") or (args[0] if args else "")
            check_call(str(model), kwargs)
            _prepare(kwargs)
            result = await function(*args, **kwargs)
            capture(result)
            return result

        return guarded

    if not getattr(litellm.completion, "_athena_guarded", False):
        litellm.completion = wrap(litellm.completion)
        litellm.completion._athena_guarded = True  # type: ignore[attr-defined]
    if not getattr(litellm.acompletion, "_athena_guarded", False):
        litellm.acompletion = awrap(litellm.acompletion)
        litellm.acompletion._athena_guarded = True  # type: ignore[attr-defined]

    # ADK binds the function at import time, so its module global needs it too.
    try:
        from google.adk.models import lite_llm as adk_lite_llm

        if not getattr(adk_lite_llm.acompletion, "_athena_guarded", False):
            adk_lite_llm.acompletion = awrap(adk_lite_llm.acompletion)
            adk_lite_llm.acompletion._athena_guarded = True  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - ADK is optional in some scripts
        pass

    _INSTALLED = True
    return True
