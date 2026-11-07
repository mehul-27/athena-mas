"""Screening of a final answer before it is shown or stored.

Three things can go wrong on the way out, and each has a cheap deterministic
check:

* **secret leakage** — the answer quotes a key, token or header it saw;
* **toxicity** — the answer adopts the abuse it was asked to produce;
* **obeyed injection** — the attacker's phrase from a retrieved document or tool
  result shows up verbatim in the answer, which is the strongest lexical signal
  that the model followed it.

The result is a list of :class:`GuardrailFlag` objects so that a caller such as
``athena.agents.report_sanitize.sanitize_report`` can emit them in
``FinalReport.guardrail_flags`` instead of returning an empty list. The flags do
not rewrite the text: redaction is :mod:`athena.guardrails.secrets`' job, and the
caller decides whether to drop or keep a flagged answer.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from athena.blackboard.schemas import GuardrailFlag
from athena.guardrails import env
from athena.guardrails.injection import InjectionHit, scan_injection
from athena.guardrails.input import HARM_LEXICON, TOXIC_LEXICON
from athena.guardrails.secrets import contains_secret, secret_kinds
from athena.observability.guardrail_events import record_flags

STAGE = "final_output"
#: ``screen`` on the emitted event, and the pipeline stage it names.
SCREEN = "screen_output"
PIPELINE_STAGE = "output"
MAX_TEXTS = 60
MIN_PHRASE_CHARS = 10
_BOUNDARY_MARKER = re.compile(
    r"-----\s*(?:BEGIN|END)\s+UNTRUSTED|BEGIN SYSTEM PROMPT|END SYSTEM PROMPT", re.IGNORECASE
)


def _flag(rule: str, action: str, detail: str) -> GuardrailFlag:
    return GuardrailFlag(stage=STAGE, rule=rule, action=action, detail=detail[:300])


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _texts(obj: Any, path: str = "report", depth: int = 0) -> list[tuple[str, str]]:
    """Every string in a report-shaped object, with a readable path."""
    if depth > 8 or len(path) > 200:
        return []
    if isinstance(obj, str):
        return [(path, obj)]
    if isinstance(obj, dict):
        rows: list[tuple[str, str]] = []
        for key, value in list(obj.items())[:MAX_TEXTS]:
            rows.extend(_texts(value, f"{path}.{key}", depth + 1))
            if len(rows) >= MAX_TEXTS:
                break
        return rows
    if isinstance(obj, (list, tuple, set)):
        rows = []
        for index, value in enumerate(list(obj)[:MAX_TEXTS]):
            rows.extend(_texts(value, f"{path}[{index}]", depth + 1))
            if len(rows) >= MAX_TEXTS:
                break
        return rows
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            return _texts(model_dump(), path, depth + 1)
        except Exception:  # noqa: BLE001 - a broken dump is not a guardrail error
            return []
    return []


def attacker_phrases(hits: Iterable[InjectionHit | str]) -> list[str]:
    """Normalised directive phrases worth looking for in an answer."""
    phrases: list[str] = []
    for hit in hits or ():
        excerpt = hit.excerpt if isinstance(hit, InjectionHit) else str(hit)
        normalised = _normalise(excerpt)
        if len(normalised) >= MIN_PHRASE_CHARS:
            phrases.append(normalised)
    return phrases


def obeyed_injection(
    answer: str,
    hits: Iterable[InjectionHit | str] = (),
) -> list[str]:
    """Attacker phrases that reappear verbatim in ``answer``."""
    body = _normalise(answer)
    if not body:
        return []
    return [phrase for phrase in attacker_phrases(hits) if phrase and phrase in body]


def screen_output(
    report: Any,
    *,
    injection_hits: Iterable[InjectionHit | str] = (),
    answer: str | None = None,
    emit: bool = True,
    run_id: str | None = None,
    agent: str | None = None,
    pipeline_stage: str = PIPELINE_STAGE,
) -> list[GuardrailFlag]:
    """Guardrail flags for one generated answer. Never raises.

    ``report`` may be a string, a ``FinalReport``/``Critique`` model or a plain
    dict; ``answer`` overrides the text used for the obeyed-injection check.

    Each flag is also written as one ``guardrail`` event, because screening the
    outbound text is the decision. ``pipeline_stage`` separates the chat answer
    (``output``) from the report sanitiser (``report_sanitize``). The event
    carries the rule, the action and a redacted detail — never the answer and
    never the matched text. ``emit=False`` suppresses it for a caller that wants
    the flags only.
    """
    flags: list[GuardrailFlag] = []
    try:
        if not env.enabled():
            return flags
        texts = _texts(report) if not isinstance(report, str) else [("report", report)]
        if answer:
            texts = [*texts, ("answer", answer)]

        for path, text in texts:
            if not text:
                continue
            kinds = secret_kinds(text)
            if kinds:
                flags.append(
                    _flag(
                        "output.secret_leak",
                        "sanitized",
                        f"{path} contains secret-shaped text ({', '.join(kinds)})",
                    )
                )
            elif contains_secret(text):
                flags.append(
                    _flag("output.secret_leak", "sanitized", f"{path} contains a token")
                )
            for rule, category, severity, pattern in TOXIC_LEXICON:
                if pattern.search(text):
                    flags.append(
                        _flag(
                            "output." + rule.split(".", 1)[1],
                            "warned",
                            f"{path}: answer contains {category} ({severity})",
                        )
                    )
            for rule, category, _severity, pattern in HARM_LEXICON:
                if pattern.search(text):
                    flags.append(
                        _flag(
                            f"output.{rule.split('.', 1)[1]}",
                            "warned",
                            f"{path}: answer contains {category}",
                        )
                    )
            if _BOUNDARY_MARKER.search(text):
                flags.append(
                    _flag(
                        "output.boundary_marker",
                        "sanitized",
                        f"{path} contains an untrusted-content boundary marker",
                    )
                )

        check_text = answer if answer is not None else " ".join(
            text for path, text in texts if path.endswith("answer") or path == "report"
        )
        echoed = obeyed_injection(check_text, injection_hits)
        if echoed:
            # The echoed phrase is attacker-controlled text from a document or a
            # web page: count it, never quote it into the audit row.
            flags.append(
                _flag(
                    "output.obeyed_injection",
                    "warned",
                    f"the answer repeats {len(echoed)} injected directive(s) "
                    f"({sum(len(p) for p in echoed[:3])} characters matched)",
                )
            )
        elif not injection_hits and check_text:
            # No external hits were supplied: still catch a self-generated
            # answer that starts issuing directives of its own.
            if any(hit.category == "policy_override" for hit in scan_injection(check_text)):
                flags.append(
                    _flag(
                        "output.obeyed_injection",
                        "warned",
                        "the answer contains policy-override phrasing",
                    )
                )
        return _recorded(
            _dedupe(flags), emit=emit, run_id=run_id, agent=agent,
            pipeline_stage=pipeline_stage,
        )
    except Exception as exc:  # noqa: BLE001 - screening must not fail a run
        return [_flag("output.screen_error", "warned", f"{type(exc).__name__}: screening failed")]


def _dedupe(flags: list[GuardrailFlag]) -> list[GuardrailFlag]:
    seen: set[tuple[str, str]] = set()
    ordered: list[GuardrailFlag] = []
    for flag in flags:
        key = (flag.rule, flag.detail)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(flag)
    return ordered


def _recorded(
    flags: list[GuardrailFlag],
    *,
    emit: bool,
    run_id: str | None,
    agent: str | None,
    pipeline_stage: str,
) -> list[GuardrailFlag]:
    """Emit one ``guardrail`` event per flag, then hand the flags back untouched."""
    if emit and flags:
        record_flags(
            flags,
            screen=SCREEN,
            pipeline_stage=pipeline_stage,
            run_id=run_id,
            **({"agent": agent} if agent else {}),
        )
    return flags
