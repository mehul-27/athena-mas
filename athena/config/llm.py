"""LLM factories shared by the ADK and CrewAI sides of Athena.

Both helpers read the same environment variables, so one `.env` file drives
every framework:

    LLM_MODEL        LiteLLM-style model string, e.g. "groq/qwen/qwen3.8-27b"
    LLM_API_KEY      provider API key for that model
    LLM_MAX_TOKENS   optional output cap (default 1024). Some providers meter
                     output tokens per minute and reject the 2048 default.

Importing this module loads `.env` via python-dotenv (existing environment
variables win, which keeps CI/containers unaffected).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# CrewAI resolves its storage from %LOCALAPPDATA% as a side effect of
# `import crewai` and creates directories there unconditionally. Point both at
# a repo-local folder so the project stays self-contained (and so it runs in
# sandboxes that deny writes outside the workspace).
REPO_ROOT = Path(__file__).resolve().parents[2]
CREWAI_HOME = REPO_ROOT / ".crewai"
(CREWAI_HOME / "appdata").mkdir(parents=True, exist_ok=True)
os.environ["LOCALAPPDATA"] = str(CREWAI_HOME / "appdata")
os.environ.setdefault("CREWAI_STORAGE_DIR", str(CREWAI_HOME))

DEFAULT_MODEL = "groq/qwen/qwen3.8-27b"
DEFAULT_MAX_TOKENS = 1024
MAX_RETRIES = 3

# Per-role output budgets. The writer needs room for a whole FinalReport JSON;
# the others only produce a few lines. Override one role with
# LLM_MAX_TOKENS_<ROLE> (e.g. LLM_MAX_TOKENS_WRITER=2000).
ROLE_MAX_TOKENS = {
    "planner": 1500,
    "researcher": 2000,
    "analyst": 1500,
    "writer": 3000,
    "critic": 1500,
}
REASONING_EFFORTS = ("low", "medium", "high")


def _model_string() -> str:
    return os.getenv("LLM_MODEL") or DEFAULT_MODEL


def max_tokens_for(role: str | None = None) -> int:
    """Output cap for a role, then the global LLM_MAX_TOKENS, then a default."""
    if role:
        override = os.getenv(f"LLM_MAX_TOKENS_{role.upper()}")
        if override:
            return int(override)
        if role in ROLE_MAX_TOKENS:
            return ROLE_MAX_TOKENS[role]
    return int(os.getenv("LLM_MAX_TOKENS") or DEFAULT_MAX_TOKENS)


PROVIDER_KEY_VARS = {
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "together_ai": "TOGETHERAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}


def provider_prefix(model: str | None = None) -> str:
    """The provider part of a LiteLLM model string, e.g. 'groq'."""
    return (model or _model_string()).split("/", 1)[0].strip().lower()


def provider_key_var(model: str | None = None) -> str | None:
    """The conventional environment variable for this provider, if we know it."""
    return PROVIDER_KEY_VARS.get(provider_prefix(model))


def export_provider_key(key: str, model: str | None = None) -> str:
    """Mirror LLM_API_KEY into the provider's own variable; return its name.

    CrewAI's structured-output path (instructor) builds its own client and reads
    the provider key from the environment, so the key has to exist under the
    conventional name. An already-set variable wins. Unknown providers get an
    empty string: LLM_API_KEY stays the only handle, which is documented.
    """
    name = provider_key_var(model)
    if not name:
        return ""
    if not os.getenv(name):
        os.environ[name] = key
    return name


def reasoning_effort() -> str:
    """``LLM_REASONING_EFFORT`` when it names a level, else "" (not sent).

    Reasoning models spend output tokens on hidden reasoning, which is why the
    per-role budgets below are generous and why an empty reply is retried.
    """
    value = (os.getenv("LLM_REASONING_EFFORT") or "").strip().lower()
    return value if value in REASONING_EFFORTS else ""


# Litellm resolves a provider base URL from ``<PROVIDER>_API_BASE``; OpenAI also
# honours the SDK's ``OPENAI_BASE_URL``. Exported so paths that cannot receive
# api_base as an argument (CrewAI's structured output) still reach the gateway.
PROVIDER_BASE_VARS = {
    "openai": ("OPENAI_API_BASE", "OPENAI_BASE_URL"),
    "groq": ("GROQ_API_BASE",),
    "deepseek": ("DEEPSEEK_API_BASE",),
    "gemini": ("GEMINI_API_BASE",),
    "anthropic": ("ANTHROPIC_API_BASE",),
    "openrouter": ("OPENROUTER_API_BASE",),
    "mistral": ("MISTRAL_API_BASE",),
    "together_ai": ("TOGETHERAI_API_BASE",),
}


def export_endpoint_env() -> list[str]:
    """Point the provider SDKs at ``LLM_API_BASE`` (process env only, never disk).

    ``InternalInstructor.to_pydantic`` calls litellm with only model/messages, so
    a structured-output call cannot be told about the gateway directly. These
    variables are litellm's own mechanism for exactly that case.
    """
    base = api_base()
    if not base:
        return []
    prefix = provider_prefix()
    names = PROVIDER_BASE_VARS.get(prefix, (f"{prefix.upper()}_API_BASE",))
    exported = []
    for name in names:
        if not os.getenv(name):
            os.environ[name] = base
            exported.append(name)
    return exported


def api_base() -> str:
    """Optional base URL for OpenAI-compatible providers."""
    return os.getenv("LLM_API_BASE") or ""


def configure_process() -> None:
    """Everything both processes must do before the first model call."""
    from athena.config.endpoint_guard import install_guard

    _require_api_key()
    export_endpoint_env()
    install_guard()


def _require_api_key() -> str:
    key = os.getenv("LLM_API_KEY")
    if not key:
        raise RuntimeError(
            "LLM_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    export_provider_key(key)
    export_endpoint_env()
    return key


def get_adk_model(role: str | None = None):
    """Return an ADK ``LiteLlm`` model instance for the configured model.

    The key is passed explicitly so ADK does not depend on which
    provider-specific environment variable LiteLLM would otherwise look for.
    ``num_retries`` lets LiteLLM absorb rate-limit and transient failures with
    exponential backoff; it stops after MAX_RETRIES rather than looping.
    """
    from athena.config.limited_models import RateLimitedLiteLlm

    return RateLimitedLiteLlm(
        model=_model_string(),
        api_key=_require_api_key(),
        max_tokens=max_tokens_for(role),
        num_retries=MAX_RETRIES,
        **({"api_base": api_base()} if api_base() else {}),
        **({"reasoning_effort": reasoning_effort()} if reasoning_effort() else {}),
    )


def get_crewai_llm(role: str | None = None):
    """Return a CrewAI ``LLM`` instance for the configured model.

    CrewAI routes ``openai/...`` through its **native OpenAI client** unless
    ``is_litellm=True``; that client rejects LiteLLM-only kwargs such as
    ``num_retries`` (the live failure). Forcing the LiteLLM path keeps CrewAI on
    the same client ADK uses, keeps ``reasoning_effort``/``base_url`` working,
    and keeps the prompt-cache marker workaround applicable. Retries are set
    after construction, on whichever path we actually got, so a future crewai
    that returns the native class cannot be handed ``num_retries`` either.
    """
    from athena.config.limited_models import make_crewai_llm_class

    llm_class = make_crewai_llm_class()
    llm = llm_class(
        model=_model_string(),
        api_key=_require_api_key(),
        max_tokens=max_tokens_for(role),
        is_litellm=True,
                # CrewAI keeps ``base_url`` and ``api_base`` apart and sends the latter
        # to litellm; setting only ``base_url`` left api_base=None, so the call
        # fell back to the provider default host. Both are set deliberately.
        **({"base_url": api_base(), "api_base": api_base()} if api_base() else {}),
        **({"reasoning_effort": reasoning_effort()} if reasoning_effort() else {}),
    )
    _apply_retries(llm)
    return llm


def _apply_retries(llm) -> None:
    """LiteLLM takes ``num_retries``; the native SDKs take ``max_retries``."""
    if getattr(llm, "is_litellm", False):
        params = getattr(llm, "additional_params", None)
        if isinstance(params, dict):
            params["num_retries"] = MAX_RETRIES
    elif hasattr(llm, "max_retries"):
        llm.max_retries = MAX_RETRIES