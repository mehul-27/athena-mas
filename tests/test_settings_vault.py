"""The vault and provider profiles: no secret may ever leak."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athena.settings import profiles as profiles_module
from athena.settings.profiles import (
    Profile,
    ProfileError,
    delete,
    env_for_profile,
    find,
    key_name,
    upsert,
    validate_api_base,
)
from athena.settings.vault import FernetVault, MemoryStore, VaultError, set_vault

SENTINEL = "sk-test-SENTINEL-do-not-print-0123456789"


@pytest.fixture(autouse=True)
def _clean_vault():
    set_vault(MemoryStore())
    yield
    set_vault(None)


@pytest.fixture()
def store(tmp_path) -> Path:
    return tmp_path / "profiles.json"


def _profile(**kwargs) -> Profile:
    base = dict(name="Test", api_base="https://gateway.example/v1",
                model="openai/fake-model")
    base.update(kwargs)
    return Profile(**base)


# --- vault ------------------------------------------------------------------


def test_fernet_vault_round_trip(tmp_path) -> None:
    vault = FernetVault(tmp_path)

    vault.set("athena/profile/p1/llm", SENTINEL)

    assert vault.has("athena/profile/p1/llm")
    assert vault.get("athena/profile/p1/llm") == SENTINEL
    raw = (tmp_path / "secrets.enc.json").read_text(encoding="utf-8")
    assert SENTINEL not in raw, "the value must be encrypted at rest"


def test_fernet_vault_survives_a_new_instance(tmp_path) -> None:
    FernetVault(tmp_path).set("k", SENTINEL)
    assert FernetVault(tmp_path).get("k") == SENTINEL


def test_vault_delete_and_empty_guard(tmp_path) -> None:
    vault = FernetVault(tmp_path)
    vault.set("k", SENTINEL)
    vault.delete("k")

    assert vault.has("k") is False
    assert vault.get("k") is None
    with pytest.raises(VaultError):
        vault.set("k", "")


def test_memory_store_mirrors_the_interface() -> None:
    vault = MemoryStore()
    vault.set("a", SENTINEL)

    assert vault.has("a") and vault.get("a") == SENTINEL
    vault.delete("a")
    assert not vault.has("a")


# --- profiles and validation ------------------------------------------------


def test_url_validation() -> None:
    assert validate_api_base("https://gateway.example/v1/") == "https://gateway.example/v1"
    assert validate_api_base("http://localhost:8080/v1") == "http://localhost:8080/v1"

    with pytest.raises(ProfileError):
        validate_api_base("http://gateway.example/v1")
    with pytest.raises(ProfileError):
        validate_api_base("https://user:pass@gateway.example/v1")
    with pytest.raises(ProfileError):
        validate_api_base("ftp://gateway.example")


def test_profile_requires_a_name_and_model() -> None:
    with pytest.raises(ProfileError):
        _profile(name="").validate()
    with pytest.raises(ProfileError):
        _profile(model="").validate()


def test_searxng_needs_an_endpoint() -> None:
    with pytest.raises(ProfileError):
        _profile(search_provider="searxng").validate()


def test_upsert_find_delete_round_trip(store: Path) -> None:
    profile = upsert(_profile(), store)

    assert find(profile.id, store).model == "openai/fake-model"
    profile.model = "openai/other"
    upsert(profile, store)
    assert find(profile.id, store).model == "openai/other"

    delete(profile.id, store)
    assert find(profile.id, store) is None


def test_public_view_never_carries_the_key(store: Path) -> None:
    profile = upsert(_profile(), store)
    from athena.settings.vault import default_vault

    default_vault().set(key_name(profile.id, "llm"), SENTINEL)
    public = profile.public()

    assert public["has_llm_key"] is True
    assert SENTINEL not in json.dumps(public)


# --- environment builder ----------------------------------------------------


def test_env_for_profile_uses_the_vault(store: Path) -> None:
    profile = upsert(_profile(reasoning_effort="high"), store)
    from athena.settings.vault import default_vault

    default_vault().set(key_name(profile.id, "llm"), SENTINEL)

    env = env_for_profile(profile.id, path=store)

    assert env["LLM_MODEL"] == "openai/fake-model"
    assert env["LLM_API_BASE"] == "https://gateway.example/v1"
    assert env["LLM_REASONING_EFFORT"] == "high"
    assert env["LLM_API_KEY"] == SENTINEL
    assert env["SEARCH_PROVIDER"] == "duckduckgo"
    assert set(env) <= set(profiles_module.ENV_KEYS)


def test_env_for_searxng_and_search_key(store: Path) -> None:
    profile = upsert(_profile(search_provider="searxng",
                              search_endpoint="http://localhost:8888"), store)
    from athena.settings.vault import default_vault

    default_vault().set(key_name(profile.id, "search"), "search-secret")
    env = env_for_profile(profile.id, path=store)

    assert env["SEARXNG_INSTANCE"] == "http://localhost:8888"
    assert "search-secret" in json.dumps(env)


def test_env_for_unknown_profile_raises(store: Path) -> None:
    with pytest.raises(ProfileError):
        env_for_profile("nope", path=store)


def test_sentinel_never_reaches_the_profiles_file(store: Path) -> None:
    profile = upsert(_profile(), store)
    from athena.settings.vault import default_vault

    default_vault().set(key_name(profile.id, "llm"), SENTINEL)

    assert SENTINEL not in store.read_text(encoding="utf-8")
