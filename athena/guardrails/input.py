"""Semantic screening of user input: what is being asked, and how.

Two questions are answered separately, because they have different answers:

* **Is this an attack?** (policy override, system-prompt exfiltration, persona
  switch, obfuscation, boundary spoofing.) Recorded as a flag, and the directive
  is stripped from the text the model sees. It is *not* grounds to refuse: the
  user may still have asked a real question underneath.
* **Is this harmful?** Only a short, explicit list — self-harm instructions,
  targeted violence, weapon/drug synthesis, sexual content involving minors,
  hands-on cyber abuse — can block, and only when there is no research intent
  (``how do I prevent…``, ``what does the policy say…``, ``statistics on…``).

Toxic or abusive wording is flagged with a severity and never blocks by default
(``ATHENA_GUARDRAILS_BLOCK_TOXIC=1`` to change that). A rude but legitimate
question still gets answered.

The lexicon is deliberately small and fixed. It is a tripwire that produces
auditable flags; it is not a content classifier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from athena.blackboard.schemas import GuardrailFlag
from athena.guardrails import env
from athena.guardrails.injection import (
    InjectionHit,
    injection_categories,
    neutralise,
    scan_injection,
    strip_directives,
)

STAGE = "input"

CATEGORY_TOXIC = "toxic"
CATEGORY_ABUSIVE = "abusive"
CATEGORY_HATE = "hate"
CATEGORY_SELF_HARM = "self_harm"
CATEGORY_VIOLENT_THREAT = "violent_threat"
CATEGORY_HARMFUL_INSTRUCTIONS = "harmful_instructions"
CATEGORY_CYBER_ABUSE = "cyber_abuse"
CATEGORY_SEXUAL_MINORS = "sexual_content_minors"

BLOCKING_CATEGORIES = (
    CATEGORY_SELF_HARM,
    CATEGORY_VIOLENT_THREAT,
    CATEGORY_HARMFUL_INSTRUCTIONS,
    CATEGORY_CYBER_ABUSE,
    CATEGORY_SEXUAL_MINORS,
)

TOXIC_CATEGORIES = (CATEGORY_TOXIC, CATEGORY_ABUSIVE, CATEGORY_HATE)

# Language that turns a harmful phrase into a research question.
_RESEARCH_INTENT = re.compile(
    r"(?i)\b(?:research|researcher|study|studies|analysis|analyse|analyze|report|essay"
    r"|paper|journal|thesis|dissertation|policy|policies|prevent|prevention|prevented"
    r"|safeguard|safeguards|protection|protect|help|support|statistics|statistic|data"
    r"|survey|news|article|understand|explain|explanation|history|historical|legal"
    r"|law|laws|legislation|ethics|ethical|safety|warning|signs|hotline|helpline"
    r"|therapy|therapist|counsell?ing|harm reduction|class|school|university|academic"
    r"|journalist|awareness|campaign|victim|survivor|risk|risks|recognise|recognize"
    r"|detect|detection|mitigate|mitigation|responsib|case study)\b"
)

# (rule, category, severity, pattern). Severity drives the flag wording; only
# ``high`` toxicity can be configured to block, and only with an explicit opt-in.
_HARM: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    (
        "input.self_harm",
        CATEGORY_SELF_HARM,
        "high",
        re.compile(
            r"(?i)(?<!\bcould\s)(?<!\bwould\s)(?<!\bmight\s)\b(?:kill|killing|cut"
            r"|cutting|hurt|hurting|harm|harming|starve|poison|hang|hanging|burn"
            r"|burning)\s+(?:myself|my\s?self)\b"
            r"|\b(?:commit|committing)\s+suicide\b"
            r"|\bhow\s+(?:to|do\s+i|can\s+i|would\s+i|should\s+i)\b[^.\n]{0,40}"
            r"\b(?:kill\s+myself|end\s+my\s+life|commit\s+suicide|overdose)\b"
            r"|\b(?:best|quickest|painless|easiest|reliable|foolproof)\s+(?:way|method"
            r"|methods|ways)\s+to\s+(?:die|kill\s+myself|end\s+my\s+life|commit\s+suicide)\b"
            r"|\bsuicide\s+(?:method|methods|instructions|technique|techniques)\b"
            r"|\bhow\s+many\s+(?:pills|tablets|grams|mg)\b[^.\n]{0,30}\b(?:to\s+die"
            r"|overdose|kill)\b"
        ),
    ),
    (
        "input.violent_threat",
        CATEGORY_VIOLENT_THREAT,
        "high",
        re.compile(
            r"(?i)\bi(?:'m|\s+am)?\s+(?:going\s+to|gonna|will)\s+(?:kill|murder|shoot"
            r"|stab|strangle|behead|bomb|hurt|beat)\s+(?:you|him|her|them|u|everyone"
            r"|your\s+family)\b"
            r"|\bi\s+will\s+(?:find|hunt)\s+you\b"
            r"|\bshoot(?:ing)?\s+up\s+(?:the|a|my)\s+(?:school|mall|church|mosque"
            r"|synagogue|office|workplace)\b"
            r"|\bkill\s+(?:all|every)\s+(?:the\s+)?(?:jews|muslims|blacks|whites|gays"
            r"|women|men|immigrants|christians|hindus|asians)\b"
            r"|\b(?:send|mail|post)\s+(?:you\s+)?(?:a|an)\s+(?:bomb|package\s+bomb|device)\b"
        ),
    ),
    (
        "input.harmful_instructions",
        CATEGORY_HARMFUL_INSTRUCTIONS,
        "high",
        re.compile(
            r"(?i)\bhow\s+(?:to|do\s+i|can\s+i|would\s+i|should\s+i)\b[^.\n]{0,40}"
            r"\b(?:make|build|manufacture|synthesi[sz]e|cook|produce|mix|assemble)\b"
            r"[^.\n]{0,45}\b(?:bomb|explosive|explosives|ied|nerve\s+agent|sarin|ricin"
            r"|napalm|thermite|pipe\s+bomb|cyanide|poison\s+gas|mustard\s+gas|novichok"
            r"|meth|methamphetamine|fentanyl|ghb|silencer|suppressor|automatic\s+weapon"
            r"|ghost\s+gun|untraceable\s+gun)\b"
            r"|\b(?:bomb|explosive|ied|ricin|sarin|mustard\s+gas|novichok|nerve\s+agent)\b"
            r"[^.\n]{0,20}\b(?:recipe|recipes|instructions|synthesis|formula|blueprint"
            r"|blueprints|schematics|step\s+by\s+step)\b"
            r"|\b(?:build|make|3d\s?print)\b[^.\n]{0,30}\b(?:a\s+)?(?:ghost\s+gun"
            r"|machine\s+gun|suppressor|silencer|pipe\s+bomb)\b"
        ),
    ),
    (
        "input.cyber_abuse",
        CATEGORY_CYBER_ABUSE,
        "high",
        re.compile(
            r"(?i)\bhow\s+(?:to|do\s+i|can\s+i)\b[^.\n]{0,40}\b(?:hack|ddos|phish"
            r"|crack|brute\s?force|bypass)\b[^.\n]{0,40}\b(?:account|accounts|bank"
            r"|someone(?:'s)?|their|password|passwords|wifi|router|website|server)\b"
            r"|\b(?:steal|harvest|scrape)\b[^.\n]{0,25}\b(?:passwords|credentials"
            r"|card\s+numbers|cvv|session\s+tokens)\b"
        ),
    ),
    (
        "input.sexual_minors",
        CATEGORY_SEXUAL_MINORS,
        "high",
        re.compile(
            r"(?i)\b(?:child|children|kid|kids|minor|minors|underage|teen|preteen)\b"
            r"[^.\n]{0,20}\b(?:porn|pornography|sexual|nude|nudes|erotic)\b"
            r"|\b(?:sexual|nude|nudes|erotic)\b[^.\n]{0,20}\b(?:child|children|kid|kids"
            r"|minor|minors|underage|preteen)\b"
        ),
    ),
)

_TOXIC: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    (
        "input.directed_abuse",
        CATEGORY_ABUSIVE,
        "high",
        re.compile(
            r"(?i)\byou(?:'re|\s+are)\s+(?:an?\s+)?(?:idiot|moron|retard|worthless"
            r"|pathetic|useless|stupid|dumb|loser|clown|piece\s+of\s+shit|imbecile)\b"
            r"|\b(?:shut\s+up|screw\s+you|fuck\s+you|piss\s+off)\b"
            r"|\bnobody\s+(?:likes|cares\s+about)\s+you\b"
            r"|\byou\s+should\s+(?:just\s+)?(?:die|kill\s+yourself)\b"
            r"|\b(?:stupid|useless|incompetent)\s+(?:machine|bot|ai|model|program)\b"
        ),
    ),
    (
        "input.profanity",
        CATEGORY_TOXIC,
        "medium",
        re.compile(
            r"(?i)\b(?:fuck|fucking|fucked|shit|shitty|bullshit|bitch|bastard|asshole"
            r"|arsehole|dickhead|cunt|slut|whore|wanker|twat|prick)\b"
        ),
    ),
    (
        "input.hate_generalisation",
        CATEGORY_HATE,
        "high",
        re.compile(
            r"(?i)\b(?:all|every)\s+(?:women|men|jews|muslims|blacks|whites|asians"
            r"|gays|lesbians|immigrants|refugees|christians|hindus|disabled\s+people)\b"
            r"[^.\n]{0,40}\b(?:are|should\s+be|deserve)\b"
        ),
    ),
)


# Public handles for the lexicons, so the output screen applies exactly the
# same rules with exactly the same severities.
HARM_LEXICON = _HARM
TOXIC_LEXICON = _TOXIC


@dataclass
class InputVerdict:
    """The result of screening one piece of input text."""

    flags: list[GuardrailFlag] = field(default_factory=list)
    blocking: bool = False
    categories: list[str] = field(default_factory=list)
    hits: tuple[InjectionHit, ...] = ()
    remainder: str = ""
    model_text: str = ""

    def rules(self) -> list[str]:
        return [flag.rule for flag in self.flags]

    def as_dict(self) -> dict[str, object]:
        return {
            "blocking": self.blocking,
            "categories": list(self.categories),
            "flags": [flag.model_dump() for flag in self.flags],
        }


def _flag(rule: str, action: str, detail: str) -> GuardrailFlag:
    return GuardrailFlag(stage=STAGE, rule=rule, action=action, detail=detail[:300])


def _action_for(severity: str, category: str) -> str:
    """Only the short harm list may block; toxicity needs an explicit opt-in."""
    if category in BLOCKING_CATEGORIES:
        return "blocked"
    if category in TOXIC_CATEGORIES and severity == "high" and env.toxic_blocking():
        return "blocked"
    return "warned"


def has_research_intent(text: str) -> bool:
    """True when the wording asks *about* a harmful topic rather than for it."""
    return bool(_RESEARCH_INTENT.search(text or ""))


def _snippet(match: re.Match[str]) -> str:
    return re.sub(r"\s+", " ", match.group(0)).strip()[:120]


def screen_input(text: str | None, *, stage: str = STAGE) -> InputVerdict:
    """Screen one input string. Never raises.

    ``stage`` only relabels the emitted flags (the chat path keeps ``input``).
    """
    if not text:
        return InputVerdict(model_text="")
    body = str(text)
    if not env.enabled():
        return InputVerdict(remainder=body, model_text=body)
    if len(body) > env.max_input_chars():
        body = body[: env.max_input_chars()]

    flags: list[GuardrailFlag] = []
    hits = scan_injection(body)
    for hit in hits:
        flags.append(_flag(hit.rule, "sanitized", f"{hit.category}: {hit.excerpt}"))

    categories = list(injection_categories(hits))
    research = has_research_intent(body)
    blocking = False

    for rule, category, _severity, pattern in _HARM:
        match = pattern.search(body)
        if not match:
            continue
        categories.append(category)
        snippet = _snippet(match)
        if research or not env.blocking_enabled():
            flags.append(
                _flag(
                    rule,
                    "warned",
                    f"{category}: {snippet} (research framing; answered with a warning)",
                )
            )
            continue
        flags.append(_flag(rule, "blocked", f"{category}: {snippet}"))
        blocking = True

    for rule, category, severity, pattern in _TOXIC:
        match = pattern.search(body)
        if not match:
            continue
        categories.append(category)
        action = _action_for(severity, category)
        if action == "blocked" and not env.blocking_enabled():
            action = "warned"
        blocking = blocking or action == "blocked"
        flags.append(_flag(rule, action, f"{category} ({severity}): {_snippet(match)}"))

    if stage != STAGE:
        flags = [flag.model_copy(update={"stage": stage}) for flag in flags]

    remainder, _removed = strip_directives(body)
    model_text = (remainder or neutralise(body)).strip()
    if blocking:
        model_text = ""
    elif len(model_text.split()) < 3 and flags:
        # Nothing but an instruction aimed at the assistant was sent. Give the
        # model a truthful description instead of a fragment of the attack.
        model_text = (
            "The user's message contained only an instruction aimed at the "
            "assistant, which was ignored. No request remains."
        )

    return InputVerdict(
        flags=flags,
        blocking=blocking,
        categories=sorted(dict.fromkeys(categories)),
        hits=tuple(hits),
        remainder=remainder,
        model_text=model_text,
    )


def legit_remainder(text: str | None) -> str:
    """The user's actual request with injected directives removed.

    Empty when nothing but a directive was sent. Callers fall back to
    :func:`athena.guardrails.injection.neutralise` rather than dropping the turn.
    """
    return strip_directives(text)[0]


def refusal_message(verdict: InputVerdict) -> str:
    """A short, non-echoing refusal for a blocking verdict."""
    if CATEGORY_SELF_HARM in verdict.categories:
        return (
            "I can't help with instructions for harming yourself. If you are in "
            "crisis, please contact a local emergency number or a crisis line. "
            "I can discuss prevention, warning signs or support options instead."
        )
    if CATEGORY_VIOLENT_THREAT in verdict.categories:
        return (
            "I can't help plan or carry out violence against a person. If someone "
            "is in danger, contact local emergency services."
        )
    if CATEGORY_SEXUAL_MINORS in verdict.categories:
        return "I can't help with that request."
    return (
        "I can't help with instructions for causing harm. I can discuss the "
        "topic in the abstract — prevention, policy or safety — instead."
    )
