"""Account attribution and the 30-run plan.

The guarantees under test are the ones that keep this benchmark defensible:
labelled accounts only, comparable conditions, static allocation, and no key
material anywhere near a record.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation import accounts, schema

REPO = Path(__file__).resolve().parents[1]


def _account(label: str, key: str, base: str = "https://api.groq.com/openai/v1",
             model: str = "groq/qwen/qwen3.8-27b") -> accounts.Account:
    return accounts.Account(label=label, key=key, api_base=base, model=model)


# --- fingerprints ----------------------------------------------------------


def test_a_fingerprint_is_stable_and_non_reversible():
    secret = "gsk_example_secret_value_for_fingerprint_tests"
    first = accounts.account_fingerprint(secret)
    second = accounts.account_fingerprint(secret)

    assert first == second
    assert len(first) == 10
    assert secret not in first
    assert all(c in "0123456789abcdef" for c in first)


def test_two_accounts_get_different_fingerprints():
    one = _account("A", "key-one-aaaaaaaaaaaaaaaaaaaa")
    two = _account("B", "key-two-bbbbbbbbbbbbbbbbbbbb")
    assert one.account_id != two.account_id


# --- public description never carries the key ------------------------------


def test_public_description_contains_no_key_material():
    key = "gsk_live_this_must_never_appear_in_a_record_123456"
    described = _account("Groq1", key).public()
    blob = json.dumps(described)

    assert key not in blob
    assert "gsk_" not in blob
    assert set(described) == {"account_label", "account_fingerprint", "account_id"}


def test_public_description_has_only_fields_the_record_schema_accepts():
    """An extra field here is a validation failure that would lose the run."""
    described = _account("Groq1", "key-cccccccccccccccccccccc").public()
    record = schema.RunRecord.model_validate(
        {
            "run_key": "k",
            "prompt_id": "gf-01",
            "category": "general_factual",
            "difficulty": "easy",
            "persona": "novice",
            "pattern": "hierarchical",
            "context_sharing": "blackboard",
            "terminal_state": "failed",
            "latency_s": 1.0,
            **described,
        }
    )
    assert record.account_label == "Groq1"
    assert record.account_id.startswith("Groq1#")


# --- comparability --------------------------------------------------------


def test_different_endpoints_are_refused():
    with pytest.raises(accounts.AccountError, match="different endpoints"):
        accounts.verify_same_conditions(
            [
                _account("A", "key-one-aaaaaaaaaaaaaaaaaaaa"),
                _account("B", "key-two-bbbbbbbbbbbbbbbbbbbb",
                         base="https://other.example/v1"),
            ]
        )


def test_different_models_are_refused():
    with pytest.raises(accounts.AccountError, match="different models"):
        accounts.verify_same_conditions(
            [
                _account("A", "key-one-aaaaaaaaaaaaaaaaaaaa"),
                _account("B", "key-two-bbbbbbbbbbbbbbbbbbbb", model="groq/other"),
            ]
        )


def test_the_same_credential_twice_is_refused():
    """Two profiles holding one key is not two accounts."""
    with pytest.raises(accounts.AccountError, match="same credential"):
        accounts.verify_same_conditions(
            [
                _account("A", "key-shared-cccccccccccccccc"),
                _account("B", "key-shared-cccccccccccccccc"),
            ]
        )


def test_identical_conditions_are_accepted():
    accounts.verify_same_conditions(
        [
            _account("A", "key-one-aaaaaaaaaaaaaaaaaaaa"),
            _account("B", "key-two-bbbbbbbbbbbbbbbbbbbb"),
        ]
    )


# --- allocation -----------------------------------------------------------


def test_allocation_is_deterministic_regardless_of_input_order():
    pair = [_account("A", "key-one-aaaaaaaaaaaaaaaaaaaa"),
            _account("B", "key-two-bbbbbbbbbbbbbbbbbbbb")]
    keys = ["c", "a", "b", "e", "d"]

    first = accounts.allocation_for(keys, pair)
    second = accounts.allocation_for(reversed(keys), pair)

    assert first == second, "a resumed batch must keep each run's original account"


def test_allocation_covers_every_run_key_exactly_once():
    pair = [_account("A", "key-one-aaaaaaaaaaaaaaaaaaaa"),
            _account("B", "key-two-bbbbbbbbbbbbbbbbbbbb")]
    keys = [f"run-{i}" for i in range(30)]
    allocation = accounts.allocation_for(keys, pair)

    assert set(allocation) == set(keys)
    assert all(label in {"A", "B"} for label in allocation.values())


def test_allocation_splits_evenly_for_an_even_plan():
    pair = [_account("A", "key-one-aaaaaaaaaaaaaaaaaaaa"),
            _account("B", "key-two-bbbbbbbbbbbbbbbbbbbb")]
    counts: dict[str, int] = {}
    for label in accounts.allocation_for([f"r{i}" for i in range(30)], pair).values():
        counts[label] = counts.get(label, 0) + 1
    assert counts == {"A": 15, "B": 15}


def test_no_accounts_means_no_allocation():
    assert accounts.allocation_for(["a", "b"], []) == {}


# --- usage summary --------------------------------------------------------


def test_usage_is_grouped_per_account_and_never_merged():
    records = [
        {"account_label": "A", "terminal_state": "completed",
         "model_calls": 5, "tokens_in": 100, "tokens_out": 20},
        {"account_label": "A", "terminal_state": "failed",
         "model_calls": 0, "tokens_in": 0, "tokens_out": 0},
        {"account_label": "B", "terminal_state": "completed",
         "model_calls": 4, "tokens_in": 300, "tokens_out": 50},
    ]
    summary = accounts.summarise_usage(records)

    assert summary["A"]["attempts"] == 2
    assert summary["A"]["completed"] == 1
    assert summary["A"]["model_started"] == 1
    assert summary["A"]["tokens_in"] == 100
    assert summary["B"]["attempts"] == 1
    assert summary["B"]["tokens_out"] == 50


def test_records_without_attribution_are_grouped_not_guessed():
    summary = accounts.summarise_usage(
        [{"terminal_state": "completed", "tokens_in": 5, "tokens_out": 1}]
    )
    assert "(unattributed)" in summary
    assert summary["(unattributed)"]["attempts"] == 1


# --- the 30-run plan -----------------------------------------------------


def test_the_30_run_plan_exists_and_validates():
    config = schema.load_config(REPO / "evaluation" / "hierarchical_30.json")
    entries = config.entries()

    assert len(entries) == 30
    assert config.plan.target_runs == 30
    assert config.plan.scope == "focused"


def test_the_30_run_plan_shape():
    entries = schema.load_config(REPO / "evaluation" / "hierarchical_30.json").entries()

    tiers = {}
    for entry in entries:
        tiers[entry.tier] = tiers.get(entry.tier, 0) + 1
    assert tiers == {"core": 12, "breadth": 12, "variance": 6}

    assert len({e.run_key for e in entries}) == 30
    assert len({e.prompt_id for e in entries}) == 24
    assert {e.pattern for e in entries} == {"hierarchical"}
    assert {e.context_sharing for e in entries} == {"blackboard"}


def test_the_30_run_plan_covers_all_twelve_categories():
    dataset = schema.load_dataset(REPO / "evaluation" / "dataset.json")
    by_id = {item.id: item.category for item in dataset.items}
    entries = schema.load_config(REPO / "evaluation" / "hierarchical_30.json").entries()

    categories = {by_id[e.prompt_id] for e in entries}
    assert len(categories) == 12


def test_every_prompt_id_exists_in_the_dataset():
    dataset = schema.load_dataset(REPO / "evaluation" / "dataset.json")
    known = {item.id for item in dataset.items}
    entries = schema.load_config(REPO / "evaluation" / "hierarchical_30.json").entries()

    unknown = [e.prompt_id for e in entries if e.prompt_id not in known]
    assert unknown == []


def test_the_36_run_plan_is_preserved():
    """The existing benchmark must not be disturbed by the new one."""
    config = schema.load_config(REPO / "evaluation" / "hierarchical_36.json")
    assert len(config.entries()) == 36
    assert config.plan.target_runs == 36


def test_variance_entries_repeat_core_prompts_and_claim_no_seed():
    entries = schema.load_config(REPO / "evaluation" / "hierarchical_30.json").entries()
    core = {e.prompt_id for e in entries if e.tier == "core"}
    variance = [e for e in entries if e.tier == "variance"]

    assert len(variance) == 6
    assert all(e.prompt_id in core for e in variance)
    assert all(e.seed is None for e in entries), (
        "no seed reaches the provider, so no entry may claim one"
    )
