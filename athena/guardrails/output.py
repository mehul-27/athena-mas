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

STAGE = "final_output"
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
) -> list[GuardrailFlag]:
    """Guardrail flags for one generated answer. Never raises.

    ``report`` may be a string, a ``FinalReport``/``Critique`` model or a plain
    dict; ``answer`` overrides the text used for the obeyed-injection check.
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
            flags.append(
                _flag(
                    "output.obeyed_injection",
                    "warned",
                    "the answer repeats an injected directive: "
                    + "; ".join(phrase[:60] for phrase in echoed[:3]),
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
        return _dedupe(flags)
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
