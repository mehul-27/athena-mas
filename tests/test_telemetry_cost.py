"""Offline tests for the bundled price table and the cost estimator."""

from __future__ import annotations

import json

from athena.observability.cost import (
    DEFAULT_PRICING_PATH,
    LIST_PRICE,
    UNPRICED,
    estimate_cost,
    load_pricing,
    match_model,
    pricing_meta,
    sum_costs,
)


def test_bundled_table_is_cited_and_dated() -> None:
    assert DEFAULT_PRICING_PATH.exists(), "data/pricing.json is missing"
    meta = pricing_meta()

    assert meta["entries"] > 20
    assert meta["as_of"], "the table must state the date it was read"
    assert meta["source"].startswith("http")
    assert meta["currency"] == "USD"
    assert meta["unit"] == "usd_per_million_tokens"
    assert "guess" in meta["note"].lower(), "the note must say unknown models are not guessed"


def test_every_entry_has_rates_and_a_source() -> None:
    table = load_pricing()
    for entry in table["models"]:
        assert entry["match"]
        assert float(entry["input_per_mtok"]) >= 0
        assert float(entry["output_per_mtok"]) >= 0
        assert str(entry["source"]).startswith("http")


def test_known_model_is_priced_from_list_prices() -> None:
    estimate = estimate_cost("llama-3.3-70b-versatile", 1_000_000, 0)

    assert estimate.cost_usd == 0.59
    assert estimate.cost_basis == LIST_PRICE
    assert estimate.matched_pattern == "llama-3.3-70b-versatile"
    assert estimate.provider == "groq"
    assert estimate.priced is True

    both = estimate_cost("llama-3.3-70b-versatile", 1_000_000, 1_000_000)
    assert both.cost_usd == 1.38


def test_provider_prefixed_and_versioned_ids_match() -> None:
    assert estimate_cost("groq/llama-3.3-70b-versatile", 1, 1).cost_usd is not None
    assert estimate_cost("openai/gpt-4o-mini-2024-07-18", 1, 1).matched_pattern == "gpt-4o-mini*"
    assert estimate_cost("GPT-4O-MINI", 1, 1).cost_usd is not None
    assert match_model("deepseek/deepseek-reasoner")["provider"] == "deepseek"


def test_specific_pattern_wins_over_the_broader_one() -> None:
    mini = estimate_cost("gpt-4o-mini-2024-07-18", 1_000_000, 0)
    full = estimate_cost("gpt-4o-2024-11-20", 1_000_000, 0)

    assert mini.cost_usd == 0.15
    assert full.cost_usd == 2.5


def test_unknown_model_is_unpriced_and_never_guessed() -> None:
    estimate = estimate_cost("some-local-model-7b", 5_000, 500)

    assert estimate.cost_usd is None
    assert estimate.cost_basis == UNPRICED
    assert estimate.matched_pattern is None
    assert "not in the bundled price table" in estimate.reason
    assert estimate.as_dict()["cost_usd"] is None


def test_missing_model_is_unpriced_with_a_reason() -> None:
    for value in (None, "", "   "):
        estimate = estimate_cost(value, 10, 10)
        assert estimate.cost_usd is None
        assert estimate.cost_basis == UNPRICED
        assert estimate.reason == "no model recorded"


def test_zero_tokens_cost_zero_when_the_model_is_known() -> None:
    estimate = estimate_cost("llama-3.1-8b-instant", 0, 0)
    assert estimate.cost_usd == 0.0
    assert estimate.priced is True


def test_bad_token_values_do_not_raise() -> None:
    estimate = estimate_cost("llama-3.1-8b-instant", None, "nonsense")
    assert estimate.tokens_in == 0 and estimate.tokens_out == 0


def test_missing_table_degrades_to_unpriced(tmp_path) -> None:
    estimate = estimate_cost("llama-3.3-70b-versatile", 10, 10, path=tmp_path / "nope.json")

    assert estimate.cost_usd is None
    assert estimate.cost_basis == UNPRICED
    assert "not found" in load_pricing(tmp_path / "nope.json")["note"]


def test_broken_table_degrades_to_unpriced(tmp_path) -> None:
    broken = tmp_path / "pricing.json"
    broken.write_text("{not json", encoding="utf-8")

    assert load_pricing(broken)["entries"] == 0
    assert estimate_cost("gpt-4o-mini", 1, 1, path=broken).cost_usd is None


def test_custom_table_is_used_and_cached_by_mtime(tmp_path) -> None:
    table = tmp_path / "pricing.json"
    table.write_text(json.dumps({
        "as_of": "2020-01-01", "currency": "USD", "note": "", "source": "local",
        "models": [{"match": "house-model", "provider": "local",
                    "input_per_mtok": 1.0, "output_per_mtok": 2.0,
                    "source": "local"}],
    }), encoding="utf-8")

    assert estimate_cost("house-model", 1_000_000, 0, path=table).cost_usd == 1.0


def test_the_live_default_model_stays_unpriced_and_says_why() -> None:
    """The repo default id is not on any public price page, so it is never guessed."""
    live = "groq/qwen/qwen3.8-27b"
    estimate = estimate_cost(live, 1000, 200)

    assert estimate.cost_usd is None
    assert estimate.cost_basis == UNPRICED
    assert live.split("/", 1)[-1] in estimate.reason
    assert match_model(live) is None

    # it is documented, not silently missing, and the family member that IS
    # citable is listed with its own source
    meta = pricing_meta()
    unlisted = {str(entry.get("match")): entry for entry in meta["unlisted"]}
    assert "qwen/qwen3.8-27b" in unlisted
    record = unlisted["qwen/qwen3.8-27b"]
    assert record["provider"] == "groq"
    assert record["why"]
    assert str(record["checked_source"]).startswith("http")
    assert record["checked_as_of"] == meta["as_of"]
    family = match_model(record["nearest_listed"])
    assert family is not None and family["provider"] == "groq"
    assert str(family["source"]).startswith("http")


def test_an_unlisted_entry_never_prices_anything() -> None:
    """'unlisted' is documentation: only 'models' entries can match."""
    table = load_pricing()
    for entry in table["unlisted"]:
        for alias in [entry["match"], *entry.get("aliases", [])]:
            assert estimate_cost(alias, 1_000_000, 1_000_000).cost_usd is None
        # ...and the reference sibling is not borrowed to price it
        assert entry["nearest_listed"] not in (None, entry["match"])


def test_sum_costs_keeps_unpriced_calls_visible() -> None:
    priced = estimate_cost("llama-3.3-70b-versatile", 1_000_000, 0)
    unpriced = estimate_cost("mystery-model", 10, 10)

    only_priced = sum_costs([priced])
    assert only_priced["cost_usd"] == 0.59
    assert only_priced["cost_basis"] == LIST_PRICE

    mixed = sum_costs([priced, unpriced])
    assert mixed["cost_usd"] == 0.59
    assert mixed["cost_basis"] == "partly_unpriced"
    assert mixed["unpriced_models"] == ["mystery-model"]

    none = sum_costs([unpriced])
    assert none["cost_usd"] is None
    assert none["cost_basis"] == UNPRICED
