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
    "budget_stopped",  # the batch refused to start the run (budget)
    "skipped",  # the run was already recorded (resume)
)

MIN_ITEMS = 44
MIN_PLAN_RUNS = 48

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
    seeds: list[int] = Field(default_factory=lambda: [0, 1])
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
    seed: int = 0
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
    """The materialised stratified plan."""

    id: str = "stratified"
    target_runs: int = Field(default=MIN_PLAN_RUNS, ge=MIN_PLAN_RUNS)
    entries: list[PlanEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _stratified(self) -> "Plan":
        if len(self.entries) < MIN_PLAN_RUNS:
            raise ValueError(
                f"plan needs >= {MIN_PLAN_RUNS} runs, found {len(self.entries)}"
            )
        keys = [entry.run_key for entry in self.entries]
        if len(set(keys)) != len(keys):
            duplicates = sorted({key for key in keys if keys.count(key) > 1})
            raise ValueError(f"duplicate run_keys: {duplicates[:5]}")
        if not {"core", "variance"} <= {entry.tier for entry in self.entries}:
            raise ValueError("plan needs both a 'core' tier and a 'variance' tier")
        return self

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


class RunRecord(_Model):
    """One line of ``raw.jsonl``. Exactly one real attempt per record."""

    run_key: str
    run_id: str | None = None
    prompt_id: str
    category: str
    difficulty: str
    persona: str
    pattern: str
    context_sharing: str
    seed: int = 0
    repeat: int = 0
    tier: str = "core"
    backend: str = "in_process"
    config: dict[str, Any] = Field(
        default_factory=dict, description="The settings actually applied to this run."
    )
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
        description="'priced:<model>' when priced, 'unknown_model' when not, 'not_run'.",
    )
    tool_calls: int | None = None
    recovery_rounds: int | None = None
    revision_rounds: int | None = None
    evidence_count: int | None = None
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
        return self


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
