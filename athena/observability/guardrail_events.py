"""Guardrail decisions as ``guardrail`` events.

One place that turns a :class:`athena.blackboard.schemas.GuardrailFlag` (or a
bare ``(rule, action)`` decision) into an event, so every stage of the pipeline
records the same shape:

``extra``
    ``stage``          the flag's own stage (``input``/``tool_output``/``final_output``)
    ``pipeline_stage`` one of :data:`PIPELINE_STAGES` (``interagent``,
                       ``evidence``, ``report_sanitize`` …), which is finer than
                       the three-stage ``GuardrailStage`` literal
    ``rule``           the rule that fired
    ``action``         ``blocked`` / ``sanitized`` / ``warned``
    ``detail``         redacted, truncated explanation (never the payload)
    ``screen``         the function or endpoint that made the decision

Design constraints, all deliberate:

* **Passive.** Nothing here returns a verdict or changes one; the guardrail
  modules call it after they have decided. A monitoring failure is invisible to
  the pipeline (:meth:`Recorder.emit` already swallows store errors, and every
  function here is itself exception-proof).
* **Never the payload.** ``detail`` is a summary the rule produced (a rule name,
  a byte count, a URL). It is passed through
  :func:`athena.guardrails.secrets.redact_text` (which itself calls
  :func:`athena.observability.redact.redact`) and cut to
  :data:`MAX_DETAIL_CHARS`, so neither key material nor a whole document can
  reach the event log even if a caller passes something careless.
* **No untrusted run id.** :func:`record_flags` never takes a run id from the
  data being screened. Attribution comes from the active recorder (the run this
  process is executing) or from an operator-supplied ``run_id``. An inter-agent
  message that claims ``run_id: victim-run`` therefore cannot inject audit rows
  into another run's log.
"""

from __future__ import annotations

from typing import Any, Iterable

from athena.observability.recorder import Recorder, get_recorder

#: The six screening stages this project reports on. ``input``, ``tool_output``
#: and ``output`` overlap the three-value ``GuardrailStage`` literal; the other
#: three (``evidence``, ``interagent``, ``report_sanitize``) are decisions that
#: are not all expressed as ``GuardrailFlag.stage`` values.
PIPELINE_STAGES: tuple[str, ...] = (
    "input",
    "tool_output",
    "evidence",
    "interagent",
    "report_sanitize",
    "output",
)

GUARDRAIL_AGENT = "guardrails"
MAX_DETAIL_CHARS = 200
MAX_FLAGS_PER_CALL = 20


def describe_match(category: str, span: Any) -> str:
    """A non-echoing description of one matched span.

    The audit fact is *that* a rule fired, in which category, and how much text
    it covered — never the text itself. A short message can be the whole
    payload (``"Ignore all previous instructions"`` is 31 characters), so a
    "bounded excerpt" is still the payload whenever the match spans the input.
    ``span`` is therefore measured and dropped; only its width survives.
    """
    width = len(" ".join(str(span if span is not None else "").split()))
    return f"{category or 'match'}: matched {width} character(s)"


def _clean_detail(value: Any) -> str:
    """Redact then truncate one detail string. Never raises."""
    text = str(value if value is not None else "")
    try:
        from athena.guardrails.secrets import redact_text

        text = redact_text(text) or ""
    except Exception:  # noqa: BLE001 - redaction must never break an emission
        pass
    return text[:MAX_DETAIL_CHARS]


def _framework_of(agent: str, framework: str | None) -> str:
    if framework:
        return framework
    try:
        from athena.observability.hooks import framework_for

        return framework_for(agent)
    except Exception:  # noqa: BLE001 - a label is not worth a failure
        return "api"


def _resolve(recorder: Recorder | None, run_id: str | None) -> Recorder | None:
    if recorder is not None:
        return recorder
    if run_id:
        try:
            return Recorder.for_run(run_id)
        except Exception:  # noqa: BLE001 - no store means no audit row, not a crash
            return None
    return get_recorder()


def record_decision(
    *,
    stage: str,
    rule: str,
    action: str,
    detail: str = "",
    screen: str = "",
    pipeline_stage: str | None = None,
    run_id: str | None = None,
    agent: str = GUARDRAIL_AGENT,
    framework: str | None = None,
    recorder: Recorder | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Record one guardrail decision. Returns whether an event was written.

    ``stage`` is the flag's stage; ``pipeline_stage`` names the finer stage of
    the pipeline when it differs (``evidence``, ``interagent``,
    ``report_sanitize``). ``extra`` adds caller fields (how the run was
    attributed, which mode was validated); it is merged last and still passes
    through the same redaction, because the whole ``extra`` dict is cleaned by
    :class:`athena.observability.events.Event`.
    """
    try:
        target = _resolve(recorder, run_id)
        if target is None:
            return False
        payload: dict[str, Any] = {
            "stage": str(stage or ""),
            "rule": str(rule or ""),
            "action": str(action or ""),
            "detail": _clean_detail(detail),
            "screen": str(screen or ""),
        }
        if pipeline_stage:
            payload["pipeline_stage"] = str(pipeline_stage)[:40]
        for key, value in (extra or {}).items():
            payload.setdefault(str(key)[:40], value)
        target.emit(
            "guardrail",
            agent,
            _framework_of(agent, framework),
            ok=str(action or "") != "blocked",
            payload_summary=f"{stage}:{rule} {action}"[:300],
            extra=payload,
        )
        return True
    except Exception:  # noqa: BLE001 - monitoring must never break a screening
        return False


def record_flags(
    flags: Iterable[Any],
    *,
    screen: str,
    pipeline_stage: str | None = None,
    run_id: str | None = None,
    agent: str = GUARDRAIL_AGENT,
    framework: str | None = None,
    recorder: Recorder | None = None,
    limit: int = MAX_FLAGS_PER_CALL,
    extra: dict[str, Any] | None = None,
) -> int:
    """Record one event per flag. Returns the number of events attempted.

    ``flags`` may hold ``GuardrailFlag`` models or any object with
    ``stage``/``rule``/``action``/``detail`` attributes. An empty list writes
    nothing: a clean screening must not look like a decision. ``extra`` is
    forwarded to :func:`record_decision` for every row.
    """
    rows = list(flags or ())[:limit]
    written = 0
    for flag in rows:
        if record_decision(
            stage=str(getattr(flag, "stage", "") or ""),
            rule=str(getattr(flag, "rule", "") or ""),
            action=str(getattr(flag, "action", "") or ""),
            detail=str(getattr(flag, "detail", "") or ""),
            screen=screen,
            pipeline_stage=pipeline_stage,
            run_id=run_id,
            agent=agent,
            framework=framework,
            recorder=recorder,
            extra=extra,
        ):
            written += 1
    return written
