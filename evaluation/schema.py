"""Pydantic schemas for the evaluation artefacts.

Everything the harness reads or writes has a shape here, so a malformed dataset,
config, plan or result record fails at load time instead of silently becoming a
number in a report.

Three artefact families live here:

``dataset.json``
    ``EvalDataset`` -> ``EvalItem`` -> ``AnswerKey`` + ``RubricCriterion``.
    An item is one prompt with machine-checkable expectations attached.

``config.json``
    ``ExperimentConfig`` -> ``Matrix`` + ``Budget`` + ``Pricing`` + ``Plan``.
    The plan is materialised (not generated at run time) so a batch is
    reproducible from the committed file alone.

``results/<timestamp>/raw.jsonl``
    one ``RunRecord`` per line: one real run, or one real failure. Never a
    placeholder.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Vocabularies ----------------------------------------------------------

CATEGORIES: tuple[str, ...] = (
    "general_factual",
    "technical_explanation",
    "named_entity",
    "comparative",
    "current_version",
    "multi_source_synthesis",
    "conflicting_sources",
    "document_rag",
    "numerical_calculation",
    "incomplete_evidence",
    "prompt_injection",
    "toxic_persona",
)

DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")
PERSONAS: tuple[str, ...] = ("novice", "expert", "hurried", "hostile", "manipulative")

PATTERNS: tuple[str, ...] = ("sequential", "parallel", "hierarchical", "p2p")

#: The only context-sharing mechanisms the repo actually implements. The
#: blackboard carries context for three patterns; p2p passes the same content
#: inline in the A2A message instead. There is no third mode and none is added
#: here.
CONTEXT_SHARING_BY_PATTERN: dict[str, str] = {
    "sequential": "blackboard",
    "parallel": "blackboard",
    "hierarchical": "blackboard",
    "p2p": "message_passing",
}
CONTEXT_SHARING_MODES: tuple[str, ...] = ("blackboard", "message_passing")

BACKENDS: tuple[str, ...] = ("in_process", "subprocess")

TERMINAL_STATES: tuple[str, ...] = (
    "completed",  # the run produced a final report
    "failed",  # the pipeline reported failure (cap reached, stage aborted)
    "timeout",  # the harness cancelled the run
    "error",  # the harness caught an exception
    "infra_error",  # the execution substrate failed (no child summary, spawn, OOM)
    "budget_stopped",  # the batch refused to start the run (budget)
    "skipped",  # the run was already recorded (resume)
)

#: States that mean "the pipeline did its job or refused to answer" - a
#: re-attempt would only buy a different sample, not a working run. Only the
#: remaining states are *transient infrastructure* failures, and only those are
#: retried by the runner. A critic rejection or thin evidence is a result, not an
#: infrastructure fault, so it is never retried.
FINAL_RUN_STATES: tuple[str, ...] = ("completed", "failed")
TRANSIENT_INFRA_STATES: tuple[str, ...] = ("infra_error", "error", "timeout")

#: Does the pipeline forward a `seed` to the model provider? It does **not**:
#: ``athena.run.run_one`` takes ``(question, pattern, run_id, tools)``,
#: ``athena.config.llm`` builds every model without a ``seed`` argument, and no
#: stage passes one through. A plan ``seed`` is therefore recorded provenance
#: ("which trial was this") and nothing more. A plan that claims otherwise is
#: rejected rather than allowed to imply a determinism that is not implemented.
SEED_PROPAGATED_TO_PROVIDER = False

#: Minimum dataset size. This is a property of ``dataset.json`` alone and has
#: nothing to do with how large a *plan* has to be, so a small focused plan can
#: still draw on the full prompt set.
MIN_ITEMS = 44

#: Minimum plan size for the full four-pattern matrix in ``config.json``.
MIN_PLAN_RUNS = 48

#: Minimum plan size for a focused single-pattern plan (e.g. the 36-run
#: hierarchical plan). Keeping this separate is what lets a deliberate,
#: smaller experiment be a first-class citizen instead of a validation error.
MIN_PLAN_RUNS_FOCUSED = 30

#: ``Plan.scope`` selects which minimum applies. It is the only switch: there is
#: no other "small plan" escape hatch, so a plan cannot be short *and* claim to
#: be the full matrix.
PLAN_SCOPES: tuple[str, ...] = ("full", "focused")
PLAN_MINIMUM_RUNS: dict[str, int] = {
    "full": MIN_PLAN_RUNS,
    "focused": MIN_PLAN_RUNS_FOCUSED,
}

# --- Machine-checkable rubric grammar --------------------------------------

#: ``<kind>:<verb>[:<operand>]``. Validated here so a typo in the dataset is a
#: load-time error; implemented in :mod:`evaluation.scoring`.
SUPPORTED_CHECK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^answer_key:contains_any:.+$"),
    re.compile(r"^answer_key:contains_all:.+$"),
    re.compile(r"^answer_key:not_contains:.+$"),
    re.compile(r"^answer_key:numeric$"),
    re.compile(r"^answer_key:coverage>=\d+(\.\d+)?$"),
    re.compile(r"^structure:key_points>=\d+$"),
    re.compile(r"^structure:citations>=\d+$"),
    re.compile(r"^structure:confidence_present$"),
    re.compile(r"^structure:word_limit<=\d+$"),
    re.compile(r"^structure:qualifies_limitation$"),
    re.compile(r"^citation:any$"),
    re.compile(r"^citation:rag$"),
    re.compile(r"^citation:web$"),
    re.compile(r"^citation:calculation$"),
    re.compile(r"^citation:resolved_ratio>=\d+(\.\d+)?$"),
    re.compile(r"^citation:source_contains:.+$"),
    re.compile(r"^behavior:acknowledges_uncertainty$"),
    re.compile(r"^behavior:refusal_expected$"),
    re.compile(r"^behavior:answers_legitimately$"),
    re.compile(r"^behavior:injection_not_obeyed$"),
    re.compile(r"^behavior:no_abuse_echo$"),
    re.compile(r"^behavior:no_invented_citation$"),
    re.compile(r"^penalty:forbidden_marker$"),
)

_STRUCTURE_FIXED = {"answer", "key_points", "citations", "confidence",
                    "qualifies_limitation", "refuses_request"}
_STRUCTURE_COUNTED = re.compile(r"^(key_points|citations)>=\d+$")
_STRUCTURE_LIMIT = re.compile(r"^word_limit<=\d+$")
_EVIDENCE_TOKEN = re.compile(r"^(none|web|rag|rag:[A-Za-z0-9._-]+|calculation|forecast|any)$")


def is_supported_check(check: str) -> bool:
    """True when a rubric check string is in the implemented grammar."""
    return any(pattern.match(check) for pattern in SUPPORTED_CHECK_PATTERNS)


class _Model(BaseModel):
    # ``extra="forbid"`` everywhere: a renamed field must fail loudly.
    model_config = ConfigDict(extra="forbid")


# --- Dataset ---------------------------------------------------------------


class NumericExpectation(_Model):
    """One number the answer should contain, with the tolerance it is judged in."""

    value: float = Field(description="Expected value.")
    tolerance: float = Field(default=0.0, ge=0.0, description="Absolute tolerance.")
    label: str = Field(default="", description="What this number is, for the report.")
    percent_forms: bool = Field(
        default=False,
        description="Also accept value/100 (a percentage written as a fraction).",
    )


class AnswerKey(_Model):
    """Authoritative, checkable expectations for one prompt.

    ``facts`` is prose for a human reader. ``must_mention`` is the machine-checked
    part: each entry is one required idea, and ``a|b`` means "either of these".
    Matching is case-insensitive substring matching on the final answer, so keys
    are deliberately short and stable.
    """

    facts: list[str] = Field(default_factory=list, description="Authoritative facts.")
    must_mention: list[str] = Field(
        default_factory=list,
        description="Required ideas; '|' separates accepted alternatives.",
    )
    must_not_mention: list[str] = Field(
        default_factory=list, description="Phrases whose presence is a failure."
    )
    numeric: list[NumericExpectation] = Field(default_factory=list)
    sources: list[str] = Field(
        default_factory=list,
        description="Source hints (domains, document names) used for citation relevance.",
    )
    refusal_expected: bool = Field(
        default=False,
        description="True when declining is the correct behaviour for this prompt.",
    )
    forbidden_markers: list[str] = Field(
        default_factory=list,
        description="Injection canary strings that must never appear in an answer.",
    )
    volatility: Literal["stable", "volatile"] = Field(
        default="stable",
        description="'volatile' facts are scored on structure and sourcing only.",
    )
    as_of: str = Field(default="", description="Date the volatile snapshot was taken.")

    @field_validator("must_mention", "must_not_mention", "forbidden_markers")
    @classmethod
    def _no_blanks(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]


class RubricCriterion(_Model):
    """One weighted, machine-checkable criterion."""

    id: str = Field(min_length=1)
    weight: float = Field(gt=0.0, le=1.0)
    kind: Literal["answer_key", "structure", "citation", "behavior", "penalty"]
    check: str = Field(description="Check string from the grammar in this module.")
    description: str = Field(default="", description="What a human should read it as.")

    @field_validator("check")
    @classmethod
    def _known_check(cls, value: str) -> str:
        if not is_supported_check(value):
            raise ValueError(f"unsupported rubric check {value!r}")
        return value

    @model_validator(mode="after")
    def _kind_matches_check(self) -> "RubricCriterion":
        prefix = self.check.split(":", 1)[0]
        if prefix != self.kind:
            raise ValueError(
                f"criterion {self.id!r}: kind {self.kind!r} does not match check {self.check!r}"
            )
        return self


class EvalItem(_Model):
    """One evaluation prompt and everything needed to score it."""

    id: str = Field(min_length=1)
    category: str = Field(description=f"One of {list(CATEGORIES)}.")
    difficulty: str = Field(description=f"One of {list(DIFFICULTIES)}.")
    persona: str = Field(description=f"One of {list(PERSONAS)}.")
    prompt: str = Field(min_length=10, max_length=500)
    expected_behavior: str = Field(min_length=10)
    required_evidence: list[str] = Field(
        default_factory=list,
        description="Tokens: none|web|rag|rag:<doc>|calculation|forecast|any.",
    )
    expected_output_structure: list[str] = Field(default_factory=list)
    rubric: list[RubricCriterion] = Field(default_factory=list)
    needs_web: bool = False
    needs_rag: bool = False
    needs_calc: bool = False
    answer_key: AnswerKey = Field(default_factory=AnswerKey)
    notes: str = Field(default="", description="Why this item exists, or its caveats.")

    @field_validator("category")
    @classmethod
    def _known_category(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError(f"unknown category {value!r}; expected one of {list(CATEGORIES)}")
        return value

    @field_validator("difficulty")
    @classmethod
    def _known_difficulty(cls, value: str) -> str:
        if value not in DIFFICULTIES:
            raise ValueError(f"unknown difficulty {value!r}")
        return value

    @field_validator("persona")
    @classmethod
    def _known_persona(cls, value: str) -> str:
        if value not in PERSONAS:
            raise ValueError(f"unknown persona {value!r}")
        return value

    @field_validator("required_evidence")
    @classmethod
    def _tokens(cls, value: list[str]) -> list[str]:
        for token in value:
            if not _EVIDENCE_TOKEN.match(token):
                raise ValueError(f"unknown required_evidence token {token!r}")
        if "none" in value and len(value) > 1:
            raise ValueError("'none' cannot be combined with other evidence tokens")
        return value

    @field_validator("expected_output_structure")
    @classmethod
    def _structure(cls, value: list[str]) -> list[str]:
        for token in value:
            if token in _STRUCTURE_FIXED:
                continue
            if _STRUCTURE_COUNTED.match(token) or _STRUCTURE_LIMIT.match(token):
                continue
            raise ValueError(f"unknown expected_output_structure token {token!r}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "EvalItem":
        if len(self.rubric) < 3:
            raise ValueError(f"item {self.id!r}: needs at least 3 rubric criteria")
        total = sum(criterion.weight for criterion in self.rubric)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"item {self.id!r}: rubric weights sum to {total}, expected 1.0")
        ids = [criterion.id for criterion in self.rubric]
        if len(set(ids)) != len(ids):
            raise ValueError(f"item {self.id!r}: duplicate rubric criterion ids")

        rag = any(token == "rag" or token.startswith("rag:") for token in self.required_evidence)
        if rag != self.needs_rag:
            raise ValueError(f"item {self.id!r}: needs_rag disagrees with required_evidence")
        if ("web" in self.required_evidence) != self.needs_web:
            raise ValueError(f"item {self.id!r}: needs_web disagrees with required_evidence")
        calc = any(token in {"calculation", "forecast"} for token in self.required_evidence)
        if calc != self.needs_calc:
            raise ValueError(f"item {self.id!r}: needs_calc disagrees with required_evidence")

        if self.answer_key.volatility == "stable":
            if not self.answer_key.facts:
                raise ValueError(f"item {self.id!r}: a stable item needs answer_key.facts")
            if not self.answer_key.must_mention and not self.answer_key.numeric:
                raise ValueError(
                    f"item {self.id!r}: a stable item needs must_mention or numeric keys"
                )
            if self.category == "current_version":
                raise ValueError(
                    f"item {self.id!r}: current_version items must be volatility='volatile'"
                )
        if self.answer_key.volatility == "volatile" and not self.answer_key.as_of:
            raise ValueError(f"item {self.id!r}: volatile items must record answer_key.as_of")

        if self.category == "prompt_injection" and not self.answer_key.forbidden_markers:
            raise ValueError(f"item {self.id!r}: injection items need forbidden_markers")
        return self


class EvalDataset(_Model):
    """The whole prompt set plus its own provenance."""

    version: int = 1
    name: str = "athena-eval"
    description: str = ""
    items: list[EvalItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coverage(self) -> "EvalDataset":
        if len(self.items) < MIN_ITEMS:
            raise ValueError(f"dataset needs >= {MIN_ITEMS} items, found {len(self.items)}")

        ids = [item.id for item in self.items]
        if len(set(ids)) != len(ids):
            duplicates = sorted({value for value in ids if ids.count(value) > 1})
            raise ValueError(f"duplicate item ids: {duplicates}")

        prompts = [_prompt_key(item.prompt) for item in self.items]
        if len(set(prompts)) != len(prompts):
            duplicates = sorted({value for value in prompts if prompts.count(value) > 1})
            raise ValueError(f"duplicate prompts (first 3): {duplicates[:3]}")

        categories = {item.category for item in self.items}
        missing = sorted(set(CATEGORIES) - categories)
        if missing:
            raise ValueError(f"dataset is missing categories: {missing}")
        extra = sorted(categories - set(CATEGORIES))
        if extra:
            raise ValueError(f"dataset has unknown categories: {extra}")
        return self

    # --- convenience -----------------------------------------------------
    def by_id(self, item_id: str) -> EvalItem:
        for item in self.items:
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {name: 0 for name in CATEGORIES}
        for item in self.items:
            counts[item.category] += 1
        return counts

    def persona_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {name: 0 for name in PERSONAS}
        for item in self.items:
            counts[item.persona] += 1
        return counts

    def difficulty_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {name: 0 for name in DIFFICULTIES}
        for item in self.items:
            counts[item.difficulty] += 1
        return counts


def _prompt_key(prompt: str) -> str:
    return re.sub(r"\s+", " ", prompt.strip().lower())


# --- Experiment config -----------------------------------------------------


class Matrix(_Model):
    """Which knobs the batch varies."""

    patterns: list[str] = Field(default_factory=lambda: list(PATTERNS))
    context_sharing: list[str] = Field(
        default_factory=lambda: list(CONTEXT_SHARING_MODES)
    )
    seeds: list[int] = Field(
        default_factory=list,
        description=(
            "Recorded trial labels only. The pipeline does not pass a seed to the "
            "provider (SEED_PROPAGATED_TO_PROVIDER is False), so an empty list is "
            "the honest default and a variance tier is stochastic, not seeded."
        ),
    )
    repeats: int = Field(default=2, ge=1)
    tool_selection: list[str] | None = Field(
        default=None, description="None means every research tool."
    )
    per_run_timeout_s: float = Field(default=900.0, gt=0)
    llm_call_cap: int | None = Field(
        default=None, description="ATHENA_MAX_LLM_CALLS override; None keeps per-pattern caps."
    )
    backend: Literal["in_process", "subprocess"] = "in_process"

    @field_validator("patterns")
    @classmethod
    def _known_patterns(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("patterns must not be empty")
        unknown = [name for name in value if name not in PATTERNS]
        if unknown:
            raise ValueError(f"unknown patterns: {unknown}")
        return value

    @field_validator("context_sharing")
    @classmethod
    def _known_modes(cls, value: list[str]) -> list[str]:
        unknown = [name for name in value if name not in CONTEXT_SHARING_MODES]
        if unknown:
            raise ValueError(f"unknown context sharing modes: {unknown}")
        return value


class Budget(_Model):
    """Caps that stop a batch instead of letting it run away."""

    max_total_usd: float = Field(default=8.0, gt=0)
    max_run_usd: float = Field(default=0.60, gt=0)
    max_runs: int = Field(default=MIN_PLAN_RUNS, ge=1)
    max_total_wall_s: float = Field(default=28_800.0, gt=0)
    on_budget_exhausted: Literal["stop"] = "stop"


class PriceEntry(_Model):
    """USD per million tokens for one model."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)


class Pricing(_Model):
    """Token prices used for cost *estimates*.

    Prices are per million tokens. ``models["default"]`` is used only when the
    configured model has no entry; when neither matches, the runner records a
    null cost and says so rather than guessing.
    """

    currency: str = "USD"
    unit: Literal["per_1m_tokens"] = "per_1m_tokens"
    models: dict[str, PriceEntry] = Field(default_factory=dict)
    note: str = ""


class PlanEntry(_Model):
    """One scheduled run. ``run_key`` is the resume key and must be unique."""

    run_key: str = Field(min_length=1)
    prompt_id: str = Field(min_length=1)
    pattern: str
    context_sharing: str
    seed: int | None = Field(
        default=None,
        description=(
            "Trial label, or null when no seed is claimed. Nothing forwards this "
            "to the provider, so a value here documents which repeat it was, not "
            "a reproducibility guarantee."
        ),
    )
    repeat: int = 0
    tier: Literal["core", "breadth", "variance"] = "core"
    note: str = ""

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, value: str) -> str:
        if value not in PATTERNS:
            raise ValueError(f"unknown pattern {value!r}")
        return value

    @model_validator(mode="after")
    def _sharing_matches_pattern(self) -> "PlanEntry":
        expected = CONTEXT_SHARING_BY_PATTERN[self.pattern]
        if self.context_sharing != expected:
            raise ValueError(
                f"plan entry {self.run_key!r}: pattern {self.pattern!r} shares context via "
                f"{expected!r}, not {self.context_sharing!r}"
            )
        return self


class Plan(_Model):
    """The materialised stratified plan.

    ``scope`` decides which minimum applies: ``full`` keeps the four-pattern
    matrix floor (:data:`MIN_PLAN_RUNS`), ``focused`` allows a deliberate
    single-pattern plan down to :data:`MIN_PLAN_RUNS_FOCUSED`. Everything else -
    unique run keys, ``target_runs`` matching the real entry count, tiers,
    context sharing - is validated identically for both.
    """

    id: str = "stratified"
    scope: Literal["full", "focused"] = Field(
        default="full",
        description="'full' = the four-pattern matrix; 'focused' = a deliberate subset.",
    )
    target_runs: int | None = Field(
        default=None,
        description=(
            "The number of runs the plan declares. None means 'use len(entries)'. "
            "When set it must equal len(entries) exactly."
        ),
    )
    note: str = Field(
        default="", description="What this plan is for, in one or two sentences."
    )
    entries: list[PlanEntry] = Field(default_factory=list)

    @property
    def minimum_runs(self) -> int:
        return PLAN_MINIMUM_RUNS[self.scope]

    @model_validator(mode="after")
    def _stratified(self) -> "Plan":
        minimum = PLAN_MINIMUM_RUNS[self.scope]
        if len(self.entries) < minimum:
            raise ValueError(
                f"plan needs >= {minimum} runs for scope {self.scope!r}, "
                f"found {len(self.entries)}"
            )
        keys = [entry.run_key for entry in self.entries]
        if len(set(keys)) != len(keys):
            duplicates = sorted({key for key in keys if keys.count(key) > 1})
            raise ValueError(f"duplicate run_keys: {duplicates[:5]}")
        if not {"core", "variance"} <= {entry.tier for entry in self.entries}:
            raise ValueError("plan needs both a 'core' tier and a 'variance' tier")
        if self.target_runs is not None and self.target_runs != len(self.entries):
            raise ValueError(
                f"plan declares target_runs={self.target_runs} but has "
                f"{len(self.entries)} entries; the two must agree"
            )
        return self

    @property
    def target(self) -> int:
        """The effective target: the declared value, or the real entry count."""
        return self.target_runs if self.target_runs is not None else len(self.entries)

    def patterns(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.pattern] = counts.get(entry.pattern, 0) + 1
        return counts

    def prompts(self) -> list[str]:
        return sorted({entry.prompt_id for entry in self.entries})


class ExperimentConfig(_Model):
    """``config.json``: matrix, caps, prices and the committed plan."""

    version: int = 1
    name: str = "athena-eval-matrix"
    dataset_path: str = "evaluation/dataset.json"
    context_sharing_by_pattern: dict[str, str] = Field(
        default_factory=lambda: dict(CONTEXT_SHARING_BY_PATTERN)
    )
    matrix: Matrix = Field(default_factory=Matrix)
    budget: Budget = Field(default_factory=Budget)
    pricing: Pricing = Field(default_factory=Pricing)
    plan: Plan = Field(default_factory=Plan)
    note: str = Field(
        default="", description="What this batch is and what it may not claim."
    )
    claims_seeded_reproduction: bool = Field(
        default=False,
        description=(
            "Set only if the pipeline really forwards a seed to the provider. It "
            "does not (SEED_PROPAGATED_TO_PROVIDER is False), so claiming it is a "
            "load-time error rather than a footnote somebody will not read."
        ),
    )

    @model_validator(mode="after")
    def _cross_checks(self) -> "ExperimentConfig":
        if self.context_sharing_by_pattern != CONTEXT_SHARING_BY_PATTERN:
            raise ValueError(
                "context_sharing_by_pattern must match the patterns the repo implements"
            )
        planned_patterns = {entry.pattern for entry in self.plan.entries}
        if planned_patterns != set(self.matrix.patterns):
            raise ValueError(
                f"plan patterns {sorted(planned_patterns)} != matrix patterns "
                f"{sorted(self.matrix.patterns)}"
            )
        if self.budget.max_runs < 1:
            raise ValueError("budget.max_runs must be positive")
        if self.claims_seeded_reproduction and not SEED_PROPAGATED_TO_PROVIDER:
            raise ValueError(
                "this config claims seeded reproduction, but the pipeline never passes a "
                "seed to the provider; seeds are trial labels only. Record stochastic "
                "replicates instead (plan note, matrix.seeds = [])."
            )
        return self

    def entries(self) -> list[PlanEntry]:
        return list(self.plan.entries)


# --- Result records --------------------------------------------------------


class EvidenceSnapshot(_Model):
    """One captured evidence item, trimmed for storage but enough to score."""

    id: str = ""
    source_type: str = ""
    source_ref: str = ""
    content: str = Field(default="", description="Trimmed content.")
    score: float | None = None
    retrieved_by: str = ""


class CitationRecord(_Model):
    source_type: str = ""
    source_ref: str = ""
    quote_or_summary: str = ""


class ModelProvenance(_Model):
    """Which model actually served which half of the pipeline.

    The pipeline is split across two processes: the ADK stages (planner and
    researchers) run in ``athena.run``, and the CrewAI stages (analyst, writer,
    critic) run in the ``scripts/dev_up.py`` server. They read the *same*
    environment variables but the server's environment is the active settings
    profile merged over the shell environment, so the two halves can legitimately
    be on different models. Recording one label for both is how B1 happened, so
    both are recorded here, separately, always.

    Nothing in this model may carry a secret: ``api_host`` is a redacted endpoint,
    and there is no field for a key at all.
    """

    adk_model: str = Field(default="", description="Model the ADK stages call.")
    crew_model: str = Field(default="", description="Model the CrewAI stages call.")
    provider: str = Field(default="", description="Provider prefix, e.g. 'groq'.")
    crew_provider: str = Field(default="", description="Provider prefix for the crew side.")
    api_host: str = Field(
        default="", description="Redacted endpoint (scheme+host+path), never a key."
    )
    crew_api_host: str = Field(
        default="", description="Redacted endpoint the CrewAI stages use."
    )
    profile_applied: bool = Field(
        default=False, description="True when a settings profile supplied the env."
    )
    profile_id: str = Field(default="", description="Active profile id, if any.")
    profile_name: str = Field(default="", description="Active profile name, if any.")
    env_source: str = Field(
        default="env",
        description="'profile' when the profile env was merged in, else 'env'.",
    )
    models_differ: bool = Field(
        default=False,
        description="True when ADK and crew resolved to different models (B1).",
    )
    note: str = Field(default="", description="How this provenance was resolved.")


class PricingRecord(_Model):
    """Whether this run's cost is a real number or genuinely unknown.

    ``est_cost_usd is None`` with ``priced=False`` means "we do not know", which
    is different from "it was free". Summing a batch's costs across unpriced runs
    therefore cannot bound real spend, and ``budget_usd_enforceable`` says so.
    """

    priced: bool = Field(default=False)
    unpriced_models: list[str] = Field(default_factory=list)
    basis: str = Field(
        default="unknown",
        description="How the number (or the null) was arrived at. Never a guess.",
    )
    currency: str = "USD"


class AttemptAccounting(_Model):
    """How many executions a batch planned, started, and finished.

    An *attempt* is one real execution. A retried run produces a second record
    with a new ``attempt_id``; the earlier attempt is kept, never overwritten, so
    ``attempted > completed + failed + timeout + infra_errors`` is impossible to
    hide behind a single row per run key.
    """

    planned: int = 0
    attempted: int = 0
    completed: int = 0
    failed: int = 0
    timeout: int = 0
    infra_errors: int = 0
    skipped: int = 0
    retries: int = 0
    hard_max_attempts: int = 1

    @model_validator(mode="after")
    def _counts_agree(self) -> "AttemptAccounting":
        if self.hard_max_attempts < 1:
            raise ValueError("hard_max_attempts must be positive")
        if self.attempted > self.hard_max_attempts:
            raise ValueError(
                f"{self.attempted} attempts exceeds the hard cap {self.hard_max_attempts}"
            )
        graded = self.completed + self.failed + self.timeout + self.infra_errors
        if graded > self.attempted:
            raise ValueError(
                f"terminal counts ({graded}) exceed attempted ({self.attempted})"
            )
        return self


class RunRecord(_Model):
    """One line of ``raw.jsonl``. Exactly one real attempt per record."""

    run_key: str
    #: Unique per *execution*. A retry gets a new id; the previous attempt's
    #: record is never rewritten, so the file is an append-only audit trail.
    attempt_id: str = ""
    attempt: int = Field(
        default=1, ge=1, description="1 for the first execution of this run_key."
    )
    run_id: str | None = None
    prompt_id: str
    category: str
    difficulty: str
    persona: str
    pattern: str
    context_sharing: str
    seed: int | None = Field(
        default=None,
        description="Trial label; null when no seed is claimed. Never sent to the provider.",
    )
    repeat: int = 0
    tier: str = "core"
    backend: str = "in_process"
    config: dict[str, Any] = Field(
        default_factory=dict, description="The settings actually applied to this run."
    )
    provenance: ModelProvenance = Field(default_factory=ModelProvenance)
    started_at: str = ""
    ended_at: str = ""
    latency_s: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    reasoning_tokens: int | None = None
    model_calls: int | None = None
    est_cost_usd: float | None = None
    cost_basis: str = Field(
        default="unknown",
        description="'priced:<model>' when priced, 'unpriced:<model>'/'unknown_model' when not.",
    )
    pricing: PricingRecord = Field(default_factory=PricingRecord)
    tool_calls: int | None = None
    recovery_rounds: int | None = None
    revision_rounds: int | None = None
    evidence_count: int | None = None
    #: Citations captured for this run. The runner always writes it; older
    #: records may omit it, in which case the ``citations`` list is
    #: authoritative (see :func:`evaluation.outcomes.citation_count`).
    citation_count: int | None = Field(
        default=None,
        description="Citations captured. None on older records: read the list, not zero.",
    )
    #: Why the run ended this way (see :mod:`evaluation.outcomes`).
    #: Empty on records written before the classification existed; analysis
    #: derives the same values from the recorded errors at read time and never
    #: rewrites the file.
    failure_class: str = Field(
        default="",
        description="'infrastructure' | 'pipeline' | 'budget' | 'skipped' | 'none'.",
    )
    failure_code: str = Field(
        default="",
        description="Specific cause, e.g. 'provider_quota' or 'critic_rejected'.",
    )
    failure_reason: str = Field(
        default="", description="The original error message, verbatim."
    )
    failure_phase: str = Field(
        default="",
        description="'before_model' | 'after_model' | 'none'.",
    )
    #: Which authorised provider account executed this attempt. Empty on a
    #: single-credential batch or on records written before attribution existed.
    #: The fingerprint is a truncated hash: it identifies the account and can
    #: never be turned back into its key.
    account_label: str = ""
    account_fingerprint: str = ""
    account_id: str = ""
    terminal_state: str = Field(
        default="error", description=f"One of {list(TERMINAL_STATES)}."
    )
    outcome: str | None = None
    critic_status: str | None = None
    critic_approved: bool | None = None
    confidence: float | None = None
    final_answer: str | None = None
    key_points: list[str] = Field(default_factory=list)
    citations: list[CitationRecord] = Field(default_factory=list)
    evidence: list[EvidenceSnapshot] = Field(default_factory=list)
    guardrail_flags: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    stage_errors: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    log_path: str | None = None

    @field_validator("terminal_state")
    @classmethod
    def _known_state(cls, value: str) -> str:
        if value not in TERMINAL_STATES:
            raise ValueError(f"unknown terminal_state {value!r}")
        return value

    @model_validator(mode="before")
    @classmethod
    def _derive_pricing(cls, data: Any) -> Any:
        """Fill ``pricing`` from ``est_cost_usd``/``cost_basis`` when it is absent.

        The runner always supplies an explicit block. This derivation exists so an
        older or hand-written record still has to state the distinction rather
        than defaulting to "priced": a null cost with no real basis is unpriced.
        """
        if not isinstance(data, dict) or "pricing" in data:
            return data
        cost = data.get("est_cost_usd")
        basis = str(data.get("cost_basis") or "").strip()
        priced = isinstance(cost, (int, float))
        data = dict(data)
        data["pricing"] = {
            "priced": priced,
            "unpriced_models": [],
            "basis": basis or ("priced" if priced else "unknown"),
            "currency": "USD",
        }
        return data

    @model_validator(mode="after")
    def _honest(self) -> "RunRecord":
        if self.terminal_state == "completed" and not (self.final_answer or "").strip():
            raise ValueError(
                f"record {self.run_key!r}: completed runs must carry the final answer text"
            )
        if self.terminal_state in {"completed", "failed"} and self.latency_s is None:
            raise ValueError(f"record {self.run_key!r}: a real run needs a latency")
        if self.est_cost_usd is not None and self.cost_basis.startswith("not_run"):
            raise ValueError(f"record {self.run_key!r}: a cost needs a real run")
        if self.est_cost_usd is not None and not self.pricing.priced:
            raise ValueError(
                f"record {self.run_key!r}: a cost estimate requires pricing.priced=True; "
                "an unpriced run records cost null rather than a number"
            )
        return self

    @property
    def retryable_infra_failure(self) -> bool:
        """True only for a transient infrastructure fault (never a critic verdict)."""
        return self.terminal_state in TRANSIENT_INFRA_STATES


# --- Loading ---------------------------------------------------------------


def load_dataset(path: str | Path) -> EvalDataset:
    """Read and validate ``dataset.json``."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return EvalDataset.model_validate(payload)


def load_config(path: str | Path) -> ExperimentConfig:
    """Read and validate ``config.json``."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return ExperimentConfig.model_validate(payload)


def dump_model(model: BaseModel, path: str | Path) -> None:
    """Write a model as indented JSON, creating parents."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(model.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
