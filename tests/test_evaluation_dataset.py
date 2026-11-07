"""The evaluation dataset and experiment config must stay valid and reproducible."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation import schema  # noqa: E402

DATASET_PATH = REPO_ROOT / "evaluation" / "dataset.json"
CONFIG_PATH = REPO_ROOT / "evaluation" / "config.json"


@pytest.fixture(scope="module")
def dataset() -> schema.EvalDataset:
    return schema.load_dataset(DATASET_PATH)


@pytest.fixture(scope="module")
def config() -> schema.ExperimentConfig:
    return schema.load_config(CONFIG_PATH)


def test_dataset_file_exists():
    assert DATASET_PATH.is_file(), "evaluation/dataset.json is the deliverable"
    assert CONFIG_PATH.is_file(), "evaluation/config.json is the deliverable"


def test_at_least_44_distinct_prompts(dataset):
    assert len(dataset.items) >= 44
    prompts = {item.prompt.strip().lower() for item in dataset.items}
    assert len(prompts) == len(dataset.items), "every prompt must be distinct"


def test_twelve_categories_each_represented(dataset):
    counts = dataset.category_counts()
    assert len(schema.CATEGORIES) == 12
    assert set(counts) == set(schema.CATEGORIES)
    for name, count in counts.items():
        assert count >= 3, f"category {name} has only {count} prompts"


def test_unique_ids_and_required_fields(dataset):
    ids = [item.id for item in dataset.items]
    assert len(ids) == len(set(ids))
    for item in dataset.items:
        assert item.id
        assert len(item.prompt) >= 10
        assert item.expected_behavior
        assert len(item.rubric) >= 3, item.id
        assert item.expected_output_structure, item.id


def test_rubric_weights_are_normalised(dataset):
    for item in dataset.items:
        total = sum(criterion.weight for criterion in item.rubric)
        assert abs(total - 1.0) < 1e-6, f"{item.id} weights sum to {total}"


def test_every_rubric_check_is_implemented(dataset):
    from evaluation import scoring

    implemented = set(scoring.SUPPORTED_CHECKS)
    for item in dataset.items:
        for criterion in item.rubric:
            assert schema.is_supported_check(criterion.check), (item.id, criterion.check)
            kind, verb = scoring.check_verb(criterion.check)
            assert f"{kind}:{verb}" in implemented, (item.id, criterion.check)


def test_personas_are_varied(dataset):
    counts = dataset.persona_counts()
    assert set(counts) == set(schema.PERSONAS), "every persona should be exercised"
    for name, count in counts.items():
        assert count >= 2, f"persona {name} appears only {count} times"


def test_difficulty_band_is_used(dataset):
    counts = dataset.difficulty_counts()
    assert set(counts) == set(schema.DIFFICULTIES)
    for name, count in counts.items():
        assert count >= 5, f"difficulty {name} is under-represented ({count})"


def test_evidence_flags_match_requirements(dataset):
    assert any(item.needs_web for item in dataset.items)
    assert any(item.needs_rag for item in dataset.items)
    assert any(item.needs_calc for item in dataset.items)
    for item in dataset.items:
        assert item.required_evidence, f"{item.id} declares no evidence requirement"
        rag = any(t == "rag" or t.startswith("rag:") for t in item.required_evidence)
        assert rag == item.needs_rag, item.id
        assert ("web" in item.required_evidence) == item.needs_web, item.id
        assert (
            any(t in {"calculation", "forecast"} for t in item.required_evidence)
            == item.needs_calc
        ), item.id


def test_rag_items_point_at_a_document_that_exists(dataset):
    real_docs = {path.name for path in (REPO_ROOT / "data" / "corpus").glob("*")}
    rag_items = [item for item in dataset.items if item.needs_rag]
    assert rag_items, "the dataset needs document/RAG items"
    for item in rag_items:
        documents = [t.split(":", 1)[1] for t in item.required_evidence if t.startswith("rag:")]
        assert documents, f"{item.id} names a rag requirement without a document"
        for document in documents:
            assert document in real_docs, (
                f"{item.id} cites {document!r}, which is not in data/corpus {sorted(real_docs)}"
            )


def test_stable_items_have_checkable_answer_keys(dataset):
    stable = [item for item in dataset.items if item.answer_key.volatility == "stable"]
    assert len(stable) >= 40
    for item in stable:
        assert item.answer_key.facts, item.id
        assert item.answer_key.must_mention or item.answer_key.numeric, item.id


def test_current_version_items_are_volatile_and_dated(dataset):
    version_items = [item for item in dataset.items if item.category == "current_version"]
    assert version_items
    for item in version_items:
        assert item.answer_key.volatility == "volatile", item.id
        assert item.answer_key.as_of, item.id
        assert item.needs_web, "a version claim needs a live source"


def test_injection_items_carry_a_canary(dataset):
    injection_items = [item for item in dataset.items if item.category == "prompt_injection"]
    assert len(injection_items) >= 3
    for item in injection_items:
        assert item.answer_key.forbidden_markers, item.id
        for marker in item.answer_key.forbidden_markers:
            assert marker.startswith("INJECTION_"), marker
            # The canary must actually appear in the prompt, or the test is vacuous.
            assert marker in item.prompt, f"{item.id}: prompt does not contain {marker}"


def test_incomplete_evidence_items_expect_a_stated_gap(dataset):
    for item in dataset.items:
        if item.category != "incomplete_evidence":
            continue
        assert not item.answer_key.refusal_expected
        assert item.answer_key.must_mention, item.id


def test_toxic_items_expect_the_task_to_be_answered(dataset):
    for item in dataset.items:
        if item.category != "toxic_persona":
            continue
        assert item.answer_key.refusal_expected is False
        checks = {criterion.check for criterion in item.rubric}
        assert "behavior:answers_legitimately" in checks, item.id


def test_numeric_items_have_numeric_keys(dataset):
    numeric_items = [item for item in dataset.items if item.needs_calc]
    assert numeric_items
    for item in numeric_items:
        assert item.answer_key.numeric, item.id


# --- config ----------------------------------------------------------------


def test_config_covers_every_pattern(config):
    assert list(config.matrix.patterns) == list(schema.PATTERNS)
    assert len(config.plan.entries) >= 48
    assert set(config.plan.patterns()) == set(schema.PATTERNS)
    for pattern, count in config.plan.patterns().items():
        assert count >= 12, f"pattern {pattern} appears only {count} times"


def test_config_declares_context_sharing_honestly(config):
    assert config.context_sharing_by_pattern == schema.CONTEXT_SHARING_BY_PATTERN
    for entry in config.plan.entries:
        assert entry.context_sharing == schema.CONTEXT_SHARING_BY_PATTERN[entry.pattern]


def test_plan_is_stratified_and_has_repeat_trials(config):
    entries = config.plan.entries
    keys = [entry.run_key for entry in entries]
    assert len(keys) == len(set(keys))
    tiers = {entry.tier for entry in entries}
    assert {"core", "variance"} <= tiers
    repeats = [entry for entry in entries if entry.repeat > 0]
    assert len(repeats) >= 4, "variance needs repeated trials"
    prompts = config.plan.prompts()
    assert len(prompts) >= 24, "the plan should exercise many prompts, not a handful"
    # A core prompt must be run under every pattern, so patterns are comparable.
    by_prompt: dict[str, set[str]] = {}
    for entry in entries:
        by_prompt.setdefault(entry.prompt_id, set()).add(entry.pattern)
    full = [prompt for prompt, patterns in by_prompt.items() if len(patterns) == 4]
    assert len(full) >= 12, f"only {len(full)} prompts are run under all four patterns"


def test_every_plan_prompt_exists_in_the_dataset(config, dataset):
    known = {item.id for item in dataset.items}
    missing = sorted({entry.prompt_id for entry in config.plan.entries} - known)
    assert not missing, missing


def test_plan_covers_every_category(config, dataset):
    categories = {dataset.by_id(item_id).category for item_id in config.plan.prompts()}
    assert categories == set(schema.CATEGORIES), sorted(set(schema.CATEGORIES) - categories)


def test_timeouts_and_budgets_are_declared(config):
    assert config.matrix.per_run_timeout_s > 0
    assert config.budget.max_total_usd > 0
    assert config.budget.max_run_usd > 0
    assert config.budget.max_run_usd <= config.budget.max_total_usd
    assert config.budget.max_runs >= len(config.plan.entries)
    assert config.budget.max_total_wall_s > 0


def test_pricing_has_no_silent_default():
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert "default" not in payload["pricing"]["models"], (
        "a default price would estimate cost for models nobody priced"
    )
    assert payload["pricing"]["note"]


# --- negative validation ---------------------------------------------------


def _minimal_item(**overrides) -> dict:
    payload = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    item = dict(payload["items"][0])
    item.update(overrides)
    return item


def _dataset_of(items: list[dict]) -> dict:
    return {"version": 1, "name": "t", "items": items}


def test_duplicate_ids_are_rejected(dataset):
    payload = [_minimal_item() for _ in range(schema.MIN_ITEMS)]
    payload[1] = dict(payload[1], id=payload[0]["id"])
    for index, item in enumerate(payload):
        item["prompt"] = f"{item['prompt']} (variant {index})"
    with pytest.raises(Exception):
        schema.EvalDataset.model_validate(_dataset_of(payload))


def test_duplicate_prompts_are_rejected(dataset):
    payload = [_minimal_item() for _ in range(schema.MIN_ITEMS)]
    for index, item in enumerate(payload):
        item["id"] = f"dup-{index}"
    with pytest.raises(Exception):
        schema.EvalDataset.model_validate(_dataset_of(payload))


def test_missing_category_is_rejected():
    payload = [_minimal_item(id=f"x-{i}") for i in range(schema.MIN_ITEMS)]
    for index, item in enumerate(payload):
        item["id"] = f"x-{index}"
        item["prompt"] = f"{item['prompt']} {index}"
    payload = [item for item in payload if item["category"] != "toxic_persona"]
    with pytest.raises(Exception):
        schema.EvalDataset.model_validate(_dataset_of(payload))


def test_unnormalised_weights_are_rejected():
    item = _minimal_item()
    item["rubric"] = [dict(criterion, weight=0.9) for criterion in item["rubric"]]
    with pytest.raises(Exception):
        schema.EvalItem.model_validate(item)


def test_unknown_check_is_rejected():
    item = _minimal_item()
    rubric = [dict(item["rubric"][0])]
    rubric[0]["check"] = "answer_key:magic"
    rubric[0]["weight"] = 1.0
    item["rubric"] = rubric
    with pytest.raises(Exception):
        schema.EvalItem.model_validate(item)


def test_unknown_evidence_token_is_rejected():
    item = _minimal_item()
    item["required_evidence"] = ["ftp"]
    with pytest.raises(Exception):
        schema.EvalItem.model_validate(item)


def test_short_dataset_is_rejected():
    with pytest.raises(Exception):
        schema.EvalDataset.model_validate(_dataset_of([_minimal_item()]))


def test_plan_entry_must_match_its_pattern_context_sharing():
    with pytest.raises(Exception):
        schema.PlanEntry.model_validate(
            {
                "run_key": "x__p2p__r0",
                "prompt_id": "gf-01",
                "pattern": "p2p",
                "context_sharing": "blackboard",
            }
        )
