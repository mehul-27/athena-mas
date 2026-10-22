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


def _model_string() -> str:
    return os.getenv("LLM_MODEL") or DEFAULT_MODEL


def _max_tokens() -> int:
    return int(os.getenv("LLM_MAX_TOKENS") or DEFAULT_MAX_TOKENS)


def _require_api_key() -> str:
    key = os.getenv("LLM_API_KEY")
    if not key:
        raise RuntimeError(
            "LLM_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return key


def get_adk_model():
    """Return an ADK ``LiteLlm`` model instance for the configured model.

    The key is passed explicitly so ADK does not depend on which
    provider-specific environment variable LiteLLM would otherwise look for.
    """
    from google.adk.models.lite_llm import LiteLlm

    return LiteLlm(
        model=_model_string(),
        api_key=_require_api_key(),
        max_tokens=_max_tokens(),
    )


def get_crewai_llm():
    """Return a CrewAI ``LLM`` instance for the configured model.

    CrewAI 1.15.28 stamps an internal prompt-cache marker
    (``cache_breakpoint: True``) onto its messages but only strips it on the
    Anthropic path. Strict providers — Groq among them — reject the unknown
    message property, so the subclass below removes it before the request
    leaves the process.
    """
    from crewai import LLM
    from crewai.llms.cache import CACHE_BREAKPOINT_KEY

    class _CleanMessagesLLM(LLM):
        def _format_messages_for_provider(self, messages):
            cleaned = [
                {key: value for key, value in msg.items() if key != CACHE_BREAKPOINT_KEY}
                for msg in messages
            ]
            return super()._format_messages_for_provider(cleaned)

    return _CleanMessagesLLM(
        model=_model_string(),
        api_key=_require_api_key(),
        max_tokens=_max_tokens(),
    )

