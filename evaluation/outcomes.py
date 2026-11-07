"""Classify *why* a run ended the way it did, without rewriting the record.

Why this module exists
======================
A benchmark that reports "0 % research success" when 82 % of its runs never
received a model response is not measuring research quality; it is measuring
provider availability. Both facts matter, but they are not the same fact, so
they must be reported separately.

Two consumers share one rule set:

* :func:`athena.run`'s harness (``evaluation.runner``) uses :func:`classify` to
  stamp new records with an explicit ``failure_class`` / ``failure_code`` and to
  choose the terminal state - an infrastructure fault becomes ``infra_error``
  rather than the generic ``failed``.
* :mod:`evaluation.analyze` calls the same :func:`classify` on **stored** records
  at read time. Historical ``raw.jsonl`` files are never rewritten (they are the
  audit trail), so the classification is derived from the error evidence each
  record already carries. A record written before this module existed therefore
  reports the same class as a fresh one, and the raw file stays untouched.

The rule that keeps this honest
-------------------------------
A run with zero model calls is **not** automatically an infrastructure failure.
Runs legitimately produce no model call - a rejected prompt, a budget stop, a
plan that failed validation. Infrastructure is claimed only when the recorded
error text names an infrastructure cause. Anything else with no answer is an
ordinary pipeline failure, which is a result and not a fault.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# --- Vocabulary ------------------------------------------------------------

#: Coarse classes. ``pipeline`` and ``budget`` are *results*, not faults.
FAILURE_CLASSES: tuple[str, ...] = (
    "none",  # the run completed
    "infrastructure",  # the substrate never gave the pipeline a fair chance
    "pipeline",  # the pipeline ran and did not produce an acceptable answer
    "budget",  # a configured cap stopped the run
    "skipped",  # nothing was attempted (resume)
)

#: Fine codes. Only ``infrastructure`` codes implicate the environment.
FAILURE_CODES: tuple[str, ...] = (
    "none",
    # infrastructure
    "provider_quota",  # the provider's own quota is exhausted (TPD/RPD/credits)
    "provider_unavailable",  # connection refused/reset, 5xx, overloaded
    "local_rate_limit",  # Athena's own limiter refused to wait
    "infrastructure_timeout",  # the harness or transport deadline expired
    "mcp_startup",  # an MCP stdio tool server never started
    "process_spawn",  # the child process could not be started
    # pipeline
    "critic_rejected",
    "insufficient_evidence",
    "no_final_report",
    "pipeline_error",
    # other
    "budget_exhausted",
    "skipped",
)

#: Which codes mean the environment failed rather than the pipeline.
INFRASTRUCTURE_CODES: frozenset[str] = frozenset(
    {
        "provider_quota",
        "provider_unavailable",
        "local_rate_limit",
        "infrastructure_timeout",
        "mcp_startup",
        "process_spawn",
    }
)

#: Which phase of a run the failure interrupted.
PHASES: tuple[str, ...] = (
    "none",  # no failure
    "before_model",  # nothing reached a model
    "after_model",  # at least one model call happened
)


# --- Patterns --------------------------------------------------------------
# Ordered: the first match wins, so the most specific cause is reported.
# Every pattern is matched against the record's *own* recorded error text.
_PATTERNS: tuple[tuple[str, str], ...] = (
    # The provider's own quota. Groq says "on tokens per day (TPD): Limit ...";
    # other providers phrase it as quota/credits/billing.
    (
        "provider_quota",
        r"tokens per day|\bTPD\b|rate_limit_exceeded|quota (?:exceeded|exhausted)"
        r"|insufficient[_ ]quota|exceeded your current quota|billing",
    ),
    # Athena's own sliding-window limiter refused to wait.
    ("local_rate_limit", r"RateLimitWaitError|would wait [\d.]+s for"),
    # An MCP tool server that never came up: the exact Windows failure mode.
    (
        "mcp_startup",
        r"Failed to create MCP session|toolset .{0,40}failed to load|MCPToolset"
        r"|I/O operation on closed file|mcp server (?:died|exited)",
    ),
    # A child that could not be started, or that exited without reporting.
    (
        "process_spawn",
        r"could not (?:start|spawn)|WinError|FileNotFoundError.*python"
        r"|no child summary|without writing a summary",
    ),
    # Deadlines.
    (
        "infrastructure_timeout",
        r"\btimeout after\b|\btimed out\b|TimeoutError|A2AClientTimeout|"
        r"ReadTimeout|ConnectTimeout|deadline exceeded",
    ),
    # The provider was reachable-but-broken.
    (
        "provider_unavailable",
        r"Connection (?:error|refused|reset)|ServiceUnavailable|503|502|504|"
        r"overloaded|temporarily unavailable|APIError|APIConnectionError|"
        r"InternalServerError|provider returned",
    ),
)

_BUDGET = r"budget|budget_stop|Budget reached|cap reached|max_llm_calls"

_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (code, re.compile(pattern, re.IGNORECASE)) for code, pattern in _PATTERNS
)


def _error_text(record: Mapping[str, Any]) -> str:
    """Every piece of failure evidence the record carries, as one string.

    The original messages are never discarded - :func:`classify` returns the
    first one verbatim as ``failure_reason`` so a reader can always see what the
    provider actually said.
    """
    parts: list[str] = []
    for key in ("errors", "stage_errors", "notes", "problems"):
        value = record.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, Sequence):
            parts.extend(str(item) for item in value if item)
    return " \n ".join(parts)


def _first_reason(record: Mapping[str, Any]) -> str:
    """The first recorded error, verbatim; '' when the record has none."""
    for key in ("errors", "stage_errors", "notes"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Sequence):
            for item in value:
                if str(item).strip():
                    return str(item).strip()
    return ""


def model_started(record: Mapping[str, Any]) -> bool:
    """True when at least one model call (or its token usage) was recorded.

    Deliberately conservative: ``model_calls`` is the runner's own counter, and
    token usage is the fallback for records that predate it. Either is enough to
    say a request reached a provider.
    """
    calls = record.get("model_calls")
    if isinstance(calls, (int, float)) and calls >= 1:
        return True
    for key in ("tokens_in", "tokens_out", "reasoning_tokens"):
        value = record.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return True
    return False


@dataclass(frozen=True)
class Classification:
    """What kind of ending this record describes."""

    failure_class: str
    failure_code: str
    failure_reason: str
    failure_phase: str

    @property
    def is_infrastructure(self) -> bool:
        return self.failure_class == "infrastructure"

    def as_record_fields(self) -> dict[str, str]:
        return {
            "failure_class": self.failure_class,
            "failure_code": self.failure_code,
            "failure_reason": self.failure_reason,
            "failure_phase": self.failure_phase,
        }


def _completed(record: Mapping[str, Any]) -> bool:
    return str(record.get("terminal_state") or "") == "completed"


def _has_answer(record: Mapping[str, Any]) -> bool:
    return bool(str(record.get("final_answer") or "").strip())


def classify(record: Mapping[str, Any]) -> Classification:
    """Classify one record. Pure: it never mutates ``record``.

    Order matters. A completed run is never an infrastructure failure, even if
    it logged a recoverable error: the pipeline produced an answer, so the run
    succeeded in spite of the fault. Only a run that ended without an answer can
    be attributed to the environment, and only when the error text says so.
    """
    text = _error_text(record)
    reason = _first_reason(record)
    phase = "after_model" if model_started(record) else "before_model"

    state = str(record.get("terminal_state") or "")

    if state == "skipped":
        return Classification("skipped", "skipped", reason, "none")

    # An explicit failure_class already on the record wins: the runner stamped
    # it from live evidence we no longer have, and re-deriving from a summary
    # that dropped a detail must not downgrade it.
    stamped = str(record.get("failure_class") or "")
    if stamped in FAILURE_CLASSES and stamped != "none":
        code = str(record.get("failure_code") or "none")
        if code not in FAILURE_CODES:
            code = "pipeline_error" if stamped == "pipeline" else "none"
        return Classification(
            stamped,
            code,
            str(record.get("failure_reason") or reason),
            str(record.get("failure_phase") or phase or "none"),
        )

    if _completed(record):
        # A completed run can still carry a critic rejection: that is a pipeline
        # result, never an infrastructure failure.
        if record.get("critic_approved") is False:
            return Classification("pipeline", "critic_rejected", reason, "none")
        return Classification("none", "none", "", "none")

    # --- not completed: decide why ------------------------------------
    for code, pattern in _COMPILED:
        if pattern.search(text):
            return Classification("infrastructure", code, reason, phase)

    if re.search(_BUDGET, text, re.IGNORECASE) or state == "budget_stopped":
        return Classification("budget", "budget_exhausted", reason, phase)

    if state == "timeout":
        # A timeout the harness imposed is infrastructure even if no error text
        # named it; the harness is the thing that ran out of time.
        return Classification("infrastructure", "infrastructure_timeout", reason, phase)

    if state == "infra_error":
        # Stamped by the runner without a more specific code.
        return Classification("infrastructure", "process_spawn", reason, phase)

    if state == "error":
        # A harness exception. Keep it a pipeline result unless the text above
        # identified an infrastructure cause, so an unexpected bug is not
        # excused as "the provider was down".
        return Classification("pipeline", "pipeline_error", reason, phase)

    # No answer, no answer-producing state, no infrastructure evidence: the
    # pipeline ran and did not deliver. This is a result.
    code = "no_final_report" if not _has_answer(record) else "pipeline_error"
    return Classification("pipeline", code, reason, phase)


def is_valid_completed(record: Mapping[str, Any]) -> bool:
    """A run that actually produced research output worth scoring.

    This is the denominator for research *quality*. It excludes infrastructure
    failures by construction: a run with no model call cannot have produced a
    quality signal, and averaging its zero into the quality mean would report an
    environment problem as a research defect.
    """
    if not _completed(record):
        return False
    if not _has_answer(record):
        return False
    return not classify(record).is_infrastructure


def blocked_before_model(record: Mapping[str, Any]) -> bool:
    """True when the environment stopped the run *before* any model response.

    This is the strictest reading of "infrastructure failure": the pipeline was
    never given a chance to do research, so the attempt says nothing at all about
    research quality. A run that reached a model and was cut off later is a
    different fact - it started - and it is counted separately by
    :func:`interrupted_after_model` so the two are never conflated and neither is
    hidden.
    """
    classification = classify(record)
    return classification.is_infrastructure and classification.failure_phase == "before_model"


def interrupted_after_model(record: Mapping[str, Any]) -> bool:
    """True when an infrastructure fault cut the run off after it had started."""
    classification = classify(record)
    return classification.is_infrastructure and classification.failure_phase == "after_model"



def critic_evaluable(record: Mapping[str, Any]) -> bool:
    """True when the critic actually reached a verdict on this run."""
    status = str(record.get("critic_status") or "")
    if status in {"approved", "rejected"}:
        return True
    return record.get("critic_approved") is not None


def citation_count(record: Mapping[str, Any]) -> int:
    """Citations on this record, never confusing "absent" with "zero".

    Older records may predate the ``citation_count`` field. When the count is
    missing the citations list is authoritative; only a record with neither is
    treated as having no citations.
    """
    stamped = record.get("citation_count")
    if isinstance(stamped, (int, float)):
        return int(stamped)
    citations = record.get("citations")
    if isinstance(citations, Sequence) and not isinstance(citations, (str, bytes)):
        return len(citations)
    return 0


__all__ = [
    "Classification",
    "FAILURE_CLASSES",
    "FAILURE_CODES",
    "INFRASTRUCTURE_CODES",
    "PHASES",
    "blocked_before_model",
    "citation_count",
    "classify",
    "critic_evaluable",
    "interrupted_after_model",
    "is_valid_completed",
    "model_started",
]
