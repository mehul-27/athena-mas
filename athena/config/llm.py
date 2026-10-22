"""LLM factories shared by the ADK and CrewAI sides of Athena.

Both helpers read the same two environment variables, so one `.env` file
drives every framework:

    LLM_MODEL     LiteLLM-style model string, e.g. "deepseek/deepseek-chat"
    LLM_API_KEY   provider API key for that model

Importing this module loads `.env` via python-dotenv (existing environment
variables win, which keeps CI/containers unaffected).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODEL = "deepseek/deepseek-chat"


def _model_string() -> str:
    return os.getenv("LLM_MODEL") or DEFAULT_MODEL


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

    return LiteLlm(model=_model_string(), api_key=_require_api_key())


def get_crewai_llm():
    """Return a CrewAI ``LLM`` instance for the configured model."""
    from crewai import LLM

    return LLM(model=_model_string(), api_key=_require_api_key())
