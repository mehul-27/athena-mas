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
    "planner": 400,
    "researcher": 600,
    "analyst": 500,
    "writer": 1500,
    "critic": 400,
}


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


def api_base() -> str:
    """Optional base URL for OpenAI-compatible providers."""
    return os.getenv("LLM_API_BASE") or ""


def _require_api_key() -> str:
    key = os.getenv("LLM_API_KEY")
    if not key:
        raise RuntimeError(
            "LLM_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    export_provider_key(key)
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
    )


def get_crewai_llm(role: str | None = None):
    """Return a CrewAI ``LLM`` instance for the configured model.

    The class also strips CrewAI 1.15.28's internal prompt-cache marker, which
    strict providers (Groq) reject, and puts every call behind the shared token
    limiter — see ``athena/config/limited_models.py``.
    """
    from athena.config.limited_models import make_crewai_llm_class

    return make_crewai_llm_class()(
        model=_model_string(),
        api_key=_require_api_key(),
        max_tokens=max_tokens_for(role),
        num_retries=MAX_RETRIES,
        **({"base_url": api_base()} if api_base() else {}),
    )

