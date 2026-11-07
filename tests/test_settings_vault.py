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
    with pytest.raises(ProfileError):
        _profile(search_fallback_provider="searxng").validate()
    with pytest.raises(ProfileError):
        _profile(search_fallback_provider="brave").validate()


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


def test_env_for_profile_carries_the_optional_search_fallback(store: Path) -> None:
    profile = upsert(
        _profile(
            search_fallback_provider="searxng",
            search_fallback_endpoint="http://127.0.0.1:8080",
        ),
        store,
    )
    from athena.settings.vault import default_vault

    default_vault().set(key_name(profile.id, "llm"), SENTINEL)
    env = env_for_profile(profile.id, path=store)

    assert env["SEARCH_PROVIDER"] == "duckduckgo"
    assert env["SEARCH_FALLBACK_PROVIDER"] == "searxng"
    assert env["SEARCH_FALLBACK_ENDPOINT"] == "http://127.0.0.1:8080"
    assert set(env) <= set(profiles_module.ENV_KEYS)
    assert SENTINEL not in json.dumps(
        {key: value for key, value in env.items() if key != "LLM_API_KEY"}
    )


def test_env_for_searxng_does_not_export_the_unused_search_key(store: Path) -> None:
    profile = upsert(_profile(search_provider="searxng",
                              search_endpoint="http://localhost:8888"), store)
    from athena.settings.vault import default_vault

    default_vault().set(key_name(profile.id, "search"), "search-secret")
    env = env_for_profile(profile.id, path=store)

    assert env["SEARXNG_INSTANCE"] == "http://localhost:8888"
    # DuckDuckGo and SearXNG are the supported providers; neither consumes a
    # search key, so the old Brave/Tavily secret path must not exist.
    assert "search-secret" not in json.dumps(env)


def test_env_for_unknown_profile_raises(store: Path) -> None:
    with pytest.raises(ProfileError):
        env_for_profile("nope", path=store)


def test_sentinel_never_reaches_the_profiles_file(store: Path) -> None:
    profile = upsert(_profile(), store)
    from athena.settings.vault import default_vault

    default_vault().set(key_name(profile.id, "llm"), SENTINEL)

    assert SENTINEL not in store.read_text(encoding="utf-8")


# --- launcher (scripts/dev_up.py) -------------------------------------------


def _load_dev_up():
    """Import scripts/dev_up.py without running it."""
    import importlib.util
    import sys
    from pathlib import Path as _Path

    path = _Path(__file__).resolve().parent.parent / "scripts" / "dev_up.py"
    spec = importlib.util.spec_from_file_location("_athena_dev_up", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _stub_launch(monkeypatch, dev_up, tmp_path, *, ready: bool):
    """Neutralize the child process and point the run files at tmp_path."""
    pops = []

    class FakeProcess:
        pid = 4242
        returncode = 0

        def poll(self):
            return 0 if not ready else None

    monkeypatch.setattr(dev_up, "RUN_DIR", tmp_path)
    monkeypatch.setattr(dev_up, "PID_FILE", tmp_path / "crew_a2a.pid")
    monkeypatch.setattr(dev_up, "LOG_FILE", tmp_path / "crew_a2a.log")
    answers = {"count": 0}

    def card_answers(timeout: float = 2.0) -> bool:
        answers["count"] += 1
        # The first call is dev_up's "was it already running?" probe; the server
        # is only allowed to answer after it has been started.
        return ready and answers["count"] > 1

    monkeypatch.setattr(dev_up, "card_answers", card_answers)
    monkeypatch.setattr(
        dev_up.subprocess, "Popen",
        lambda *a, **k: pops.append((a, k)) or FakeProcess(),
    )
    # "Ready" on the first poll, or an immediate early exit.
    monkeypatch.setattr(dev_up.time, "sleep", lambda seconds: None)
    return pops


def test_launcher_merges_the_active_profile_without_printing_the_key(
    monkeypatch, tmp_path, capsys
) -> None:
    dev_up = _load_dev_up()
    pops = _stub_launch(monkeypatch, dev_up, tmp_path, ready=True)
    monkeypatch.setattr(
        "athena.api.activation.active_env",
        lambda: {
            "LLM_MODEL": "groq/qwen/qwen3-32b",
            "LLM_API_KEY": SENTINEL,
            "SEARCH_PROVIDER": "duckduckgo",
            "ATHENA_SEARXNG_URL": "http://127.0.0.1:8080",
        },
    )

    code = dev_up.main()
    out = capsys.readouterr().out

    assert code == 0
    assert "active profile env: loaded" in out
    assert "key=set" in out
    assert "search=duckduckgo" in out
    assert "fallback=searxng" in out, "ATHENA_SEARXNG_URL is the fallback opt-in"
    assert SENTINEL not in out, "the launcher must never print the key"
    assert len(pops) == 1
    child_env = pops[0][1]["env"]
    assert child_env["LLM_API_KEY"] == SENTINEL, "the key must still reach the child"
    assert child_env["ATHENA_SEARXNG_URL"] == "http://127.0.0.1:8080"
    assert child_env["BLACKBOARD_DB"]
    pid = tmp_path / "crew_a2a.pid"
    assert pid.exists() and SENTINEL not in pid.read_text(encoding="utf-8")


def test_launcher_warns_when_no_key_is_configured(monkeypatch, tmp_path, capsys) -> None:
    dev_up = _load_dev_up()
    _stub_launch(monkeypatch, dev_up, tmp_path, ready=True)
    monkeypatch.setattr("athena.api.activation.active_env", lambda: {})
    for name in ("LLM_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    assert dev_up.main() == 0

    out = capsys.readouterr().out
    assert "active profile env: none" in out
    assert "key=missing" in out
    assert "warning: no LLM key" in out


def test_launcher_reports_an_early_exit(monkeypatch, tmp_path, capsys) -> None:
    dev_up = _load_dev_up()
    _stub_launch(monkeypatch, dev_up, tmp_path, ready=False)
    monkeypatch.setattr("athena.api.activation.active_env", lambda: {})

    assert dev_up.main() == 1
    assert "exited early" in capsys.readouterr().out


def test_env_summary_redacts_urls_and_never_names_a_key_value() -> None:
    from athena.tools.diagnostics import env_summary

    summary = env_summary(
        {
            "LLM_API_KEY": SENTINEL,
            "LLM_MODEL": "groq/qwen/qwen3-32b",
            "SEARCH_FALLBACK_ENDPOINT": "http://user:%s@127.0.0.1:8080" % SENTINEL,
        }
    )

    assert summary["llm_key"] == "set"
    assert summary["search_fallback"] == "none"
    assert SENTINEL not in json.dumps(summary)

