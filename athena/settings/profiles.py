"""Provider profiles: what to call, where, and whether a key is stored.

Profiles hold no secrets. A profile says which preset, base URL, model and
search provider to use; the keys live in the vault under
``athena/profile/<id>/llm`` and ``athena/profile/<id>/search``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from athena.settings.vault import SecretStore, default_vault

PROFILES_FILE = Path("data/settings/profiles.json")
PRESETS_FILE = Path("data/presets.json")
ACTIVE_FILE = Path("data/settings/active_profile.json")

SEARCH_PROVIDERS = ("duckduckgo", "searxng", "brave", "tavily")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ENV_KEYS = ("LLM_API_KEY", "LLM_API_BASE", "LLM_MODEL", "LLM_REASONING_EFFORT",
            "SEARCH_PROVIDER", "SEARXNG_INSTANCE", "BRAVE_API_KEY", "TAVILY_API_KEY")


class ProfileError(ValueError):
    """The profile is not usable as written."""


def validate_api_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ProfileError("api_base must start with https://")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise ProfileError("api_base must not contain credentials")
    if parsed.scheme == "http" and (parsed.hostname or "") not in LOCAL_HOSTS:
        raise ProfileError("http is only allowed for localhost")
    return url


def key_name(profile_id: str, kind: str = "llm") -> str:
    return f"athena/profile/{profile_id}/{kind}"


@dataclass
class Profile:
    name: str
    preset: str = "custom"
    api_base: str = ""
    model: str = ""
    reasoning_effort: str = ""
    search_provider: str = "duckduckgo"
    search_endpoint: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def validate(self) -> "Profile":
        if not (self.name or "").strip():
            raise ProfileError("name is required")
        if not (self.model or "").strip():
            raise ProfileError("model is required")
        self.api_base = validate_api_base(self.api_base)
        if self.search_provider not in SEARCH_PROVIDERS:
            raise ProfileError(f"unknown search provider {self.search_provider!r}")
        if self.search_provider == "searxng" and not self.search_endpoint:
            raise ProfileError("searxng needs a search endpoint")
        if self.search_endpoint:
            self.search_endpoint = validate_api_base(self.search_endpoint)
        if self.reasoning_effort and self.reasoning_effort not in {"low", "medium", "high"}:
            raise ProfileError("reasoning_effort must be low, medium or high")
        return self

    def public(self, vault: SecretStore | None = None) -> dict:
        store = vault or default_vault()
        data = asdict(self)
        data.pop("search_endpoint", None) if self.search_provider != "searxng" else None
        data["has_llm_key"] = store.has(key_name(self.id, "llm"))
        data["has_search_key"] = store.has(key_name(self.id, "search"))
        return data


def presets() -> list[dict]:
    if PRESETS_FILE.exists():
        return json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
    return []


def load_profiles(path: Path | None = None) -> list[Profile]:
    path = path or PROFILES_FILE
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Profile(**item) for item in raw]


def save_profiles(profiles: list[Profile], path: Path | None = None) -> None:
    path = path or PROFILES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(p) for p in profiles], indent=2), encoding="utf-8")


def upsert(profile: Profile, path: Path | None = None) -> Profile:
    profile.validate()
    profiles = load_profiles(path)
    for index, existing in enumerate(profiles):
        if existing.id == profile.id:
            profiles[index] = profile
            break
    else:
        profiles.append(profile)
    save_profiles(profiles, path)
    return profile


def delete(profile_id: str, path: Path | None = None, vault: SecretStore | None = None) -> None:
    store = vault or default_vault()
    save_profiles([p for p in load_profiles(path) if p.id != profile_id], path)
    store.delete(key_name(profile_id, "llm"))
    store.delete(key_name(profile_id, "search"))
    if active_id(path) == profile_id:
        set_active("", path)


def find(profile_id: str, path: Path | None = None) -> Profile | None:
    return next((p for p in load_profiles(path) if p.id == profile_id), None)


def active_id(path: Path | None = None) -> str:
    path = path or PROFILES_FILE
    if not ACTIVE_FILE.exists():
        return ""
    try:
        return json.loads(ACTIVE_FILE.read_text(encoding="utf-8")).get("profile_id", "")
    except ValueError:
        return ""


def set_active(profile_id: str, path: Path | None = None) -> None:
    ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(json.dumps({"profile_id": profile_id}), encoding="utf-8")


def env_for_profile(profile_id: str, vault: SecretStore | None = None,
                    path: Path | None = None) -> dict[str, str]:
    """Exactly the variables the pipeline reads. Children only, never logged."""
    profile = find(profile_id, path)
    if profile is None:
        raise ProfileError(f"no profile {profile_id!r}")
    store = vault or default_vault()

    env: dict[str, str] = {"LLM_MODEL": profile.model}
    if profile.api_base:
        env["LLM_API_BASE"] = profile.api_base
    if profile.reasoning_effort:
        env["LLM_REASONING_EFFORT"] = profile.reasoning_effort
    llm_key = store.get(key_name(profile.id, "llm"))
    if llm_key:
        env["LLM_API_KEY"] = llm_key
    env["SEARCH_PROVIDER"] = profile.search_provider
    if profile.search_endpoint:
        env["SEARXNG_INSTANCE"] = profile.search_endpoint
    search_key = store.get(key_name(profile.id, "search"))
    if search_key:
        env["BRAVE_API_KEY" if profile.search_provider == "brave" else "TAVILY_API_KEY"] = search_key
    return env
