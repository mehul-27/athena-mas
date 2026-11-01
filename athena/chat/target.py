"""Resolve which provider profile one chat turn should use."""

from __future__ import annotations

import os
from urllib.parse import urlparse

from athena.chat.models import ChatConfigError, ChatTarget
from athena.config import llm
from athena.settings import defaults
from athena.settings import profiles as profile_store
from athena.settings.vault import SecretStore, default_vault
from athena.tools import settings as tool_settings


def _local(base_url: str) -> bool:
    host = (urlparse(base_url or "").hostname or "").lower()
    return host in profile_store.LOCAL_HOSTS


def _provider(model: str) -> str:
    return llm.provider_prefix(model)


def _profile_target(profile: profile_store.Profile, vault: SecretStore) -> ChatTarget:
    key = vault.get(profile_store.key_name(profile.id, "llm")) or ""
    if not key and not _local(profile.api_base):
        raise ChatConfigError(
            f"No API key is stored for profile {profile.name!r}. "
            "Add or replace its key in Settings → Models.",
            status=409,
        )
    model = (profile.model or "").strip()
    if not model:
        raise ChatConfigError(f"Profile {profile.name!r} has no model set.", status=409)
    return ChatTarget(
        model=model,
        api_key=key,
        api_base=(profile.api_base or "").strip(),
        reasoning_effort=(profile.reasoning_effort or "").strip(),
        provider=_provider(model),
        profile_id=profile.id,
        profile_name=profile.name,
        search_provider=(profile.search_provider or "duckduckgo").lower(),
        search_endpoint=(profile.search_endpoint or "").strip(),
    )


def _env_target() -> ChatTarget:
    key = os.getenv("LLM_API_KEY") or ""
    if not key:
        raise ChatConfigError(
            "No provider is configured. Add a model in Settings → Add model, "
            "or set LLM_API_KEY in .env.",
            status=409,
        )
    model = os.getenv("LLM_MODEL") or llm.DEFAULT_MODEL
    return ChatTarget(
        model=model,
        api_key=key,
        api_base=llm.api_base(),
        reasoning_effort=llm.reasoning_effort(),
        provider=_provider(model),
        profile_id="",
        profile_name="Environment (.env)",
        search_provider=tool_settings.search_provider(),
        search_endpoint=tool_settings.searxng_instance(),
    )


def resolve_target(
    profile_id: str | None = None,
    conversation_profile: str | None = None,
    vault: SecretStore | None = None,
) -> ChatTarget:
    """Explicit profile → conversation profile → default → active → environment."""
    store = vault or default_vault()
    chosen = (profile_id or conversation_profile or "").strip()
    if not chosen:
        chosen = defaults.chat_profile_id()
    if chosen == defaults.ENV_PROFILE:
        return _env_target()
    if chosen:
        profile = profile_store.find(chosen)
        if profile is None:
            raise ChatConfigError(f"No profile {chosen!r}.", status=404)
        return _profile_target(profile, store)

    active = profile_store.active_id()
    if active:
        profile = profile_store.find(active)
        if profile is not None:
            return _profile_target(profile, store)
    return _env_target()


def search_settings(
    profile_id: str | None = None,
    conversation_profile: str | None = None,
) -> tuple[str, str]:
    """Search provider/endpoint without requiring a usable LLM key."""
    chosen = (profile_id or conversation_profile or "").strip()
    if not chosen:
        chosen = defaults.chat_profile_id()
    if chosen and chosen != defaults.ENV_PROFILE:
        profile = profile_store.find(chosen)
        if profile is not None:
            return (
                (profile.search_provider or "duckduckgo").lower(),
                (profile.search_endpoint or "").strip(),
            )
    active = profile_store.active_id()
    if active:
        profile = profile_store.find(active)
        if profile is not None:
            return (
                (profile.search_provider or "duckduckgo").lower(),
                (profile.search_endpoint or "").strip(),
            )
    return tool_settings.search_provider(), tool_settings.searxng_instance()


def models_view(vault: SecretStore | None = None) -> dict:
    """The Chat model picker: usable profiles plus the resolved default."""
    store = vault or default_vault()
    active = profile_store.active_id()
    rows = []
    for profile in profile_store.load_profiles():
        key = store.get(profile_store.key_name(profile.id, "llm")) or ""
        usable = bool(key) or _local(profile.api_base)
        reason = ""
        if not usable:
            reason = "No API key is stored for this profile."
        elif not profile.model:
            reason = "This profile has no model set."
        rows.append(
            {
                "id": profile.id,
                "name": profile.name,
                "model": profile.model,
                "api_host": (urlparse(profile.api_base or "").hostname or ""),
                "has_key": bool(key),
                "usable": usable and bool(profile.model),
                "is_active": profile.id == active,
                "reason": reason or ("" if profile.id == active else "Not the active research profile."),
                "search_provider": profile.search_provider,
            }
        )
    default: dict = {"source": "environment", "profile_id": "", "model": "", "provider": "", "configured": False}
    try:
        target = resolve_target(vault=store)
        default = {
            "source": "profile" if target.profile_id else "environment",
            "profile_id": target.profile_id,
            "model": target.model,
            "provider": target.provider,
            "configured": True,
        }
    except ChatConfigError as exc:
        default["error"] = str(exc)
    return {
        "profiles": rows,
        "active": active,
        "default": default,
        "default_profile_id": defaults.chat_profile_id(),
    }
