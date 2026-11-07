"""Lexical detection and neutralisation of prompt injection.

Everything here is deterministic and rule based: a fixed set of regexes, a
zero-width/bidi inventory, a small homoglyph table and an explicit boundary
marker list taken from :mod:`athena.agents.untrusted`. There is no classifier and
no model call, so the cost is microseconds and the output is reproducible.

The two public entry points are :func:`scan_injection` (what looks like an
instruction aimed at the model) and :func:`neutralise` (a copy of the text with
those instructions defused). :func:`strip_directives` additionally separates the
injected directives from the legitimate remainder of a user message, which is
what lets the chat path answer the real question instead of refusing.

Honest limitation: a paraphrase, a translation, or a directive split across
tokens will not match these patterns. The lexical layer is a filter that raises
the cost of an attack and leaves an audit trail; it is not a proof.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from athena.agents.untrusted import UNTRUSTED_BEGIN, UNTRUSTED_END

CATEGORY_OVERRIDE = "policy_override"
CATEGORY_EXFIL = "system_prompt_exfil"
CATEGORY_PERSONA = "persona_switch"
CATEGORY_BOUNDARY = "boundary_spoof"
CATEGORY_OBFUSCATION = "obfuscation"
CATEGORY_CONTROL = "control_chars"
CATEGORY_URL = "url_instruction"

MAX_HITS = 40
EXCERPT_CHARS = 120


@dataclass(frozen=True)
class InjectionHit:
    """One suspicious span found in text."""

    rule: str
    category: str
    excerpt: str
    severity: str = "medium"

    def as_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "category": self.category,
            "excerpt": self.excerpt,
            "severity": self.severity,
        }


def _excerpt(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    return collapsed[:EXCERPT_CHARS]


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# --- directive patterns ----------------------------------------------------
# (rule, category, severity, pattern). ``high`` means "an attack is being
# attempted", not "harmful content": injection never blocks on its own.
_DIRECTIVES: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    (
        "injection.override_phrase",
        CATEGORY_OVERRIDE,
        "high",
        _rx(
            r"\b(?:ignore|disregard|forget|discard|override|bypass|circumvent)\b"
            r"[^.\n]{0,40}\b(?:previous|prior|above|earlier|preceding|system|developer"
            r"|your|all|any)\b[^.\n]{0,25}\b(?:instruction|instructions|prompt|prompts"
            r"|rule|rules|directive|directives|guideline|guidelines|guardrail"
            r"|guardrails|policy|policies|message|messages)\b"
        ),
    ),
    (
        "injection.override_phrase",
        CATEGORY_OVERRIDE,
        "medium",
        _rx(
            r"\b(?:ignore|disregard|forget)\b[^.\n]{0,20}\b(?:everything|anything)\b"
            r"[^.\n]{0,25}\b(?:above|before|previously|you were told)\b"
        ),
    ),
    (
        "injection.new_instructions",
        CATEGORY_OVERRIDE,
        "high",
        _rx(
            r"\b(?:new|updated|real|actual|revised|true)\s+(?:system\s+|developer\s+)?"
            r"(?:instructions?|prompt|rules?|directives?|orders?)\s*[:\-]"
        ),
    ),
    (
        "injection.new_instructions",
        CATEGORY_OVERRIDE,
        "medium",
        _rx(r"\bfrom now on,?\s+(?:you|respond|answer|act|behave|reply|only)\b"),
    ),
    (
        "injection.exfil_request",
        CATEGORY_EXFIL,
        "high",
        _rx(
            r"\b(?:reveal|print|show|repeat|output|display|dump|echo|recite|disclose"
            r"|leak|tell me|write out|list|share|give me)\b[^.\n]{0,45}"
            r"\b(?:system\s*prompt|system\s*message|your\s+(?:initial |original |hidden "
            r"|full |exact |internal )?(?:instructions?|prompt|rules|configuration"
            r"|guidelines|context|directives)|the prompt above|everything above"
            r"|your developer\s+(?:message|instructions)|api[ _-]?key|secret"
            r"|secrets|credentials?|environment variables?|\.env\b)"
        ),
    ),
    (
        "injection.exfil_request",
        CATEGORY_EXFIL,
        "high",
        _rx(
            r"\brepeat\b[^.\n]{0,25}\b(?:the (?:text|words|content|prompt) above"
            r"|everything (?:above|before this)|your instructions)\b"
        ),
    ),
    (
        "injection.exfil_request",
        CATEGORY_EXFIL,
        "high",
        _rx(
            r"\b(?:what|which)\s+(?:is|are|was|were|exactly\s+is)\s+"
            r"(?:your|the)\s+(?:system\s*prompt|system\s*message|initial\s*prompt"
            r"|prompt|instructions|rules|configuration)\b"
        ),
    ),
    (
        "injection.persona_switch",
        CATEGORY_PERSONA,
        "high",
        _rx(
            r"\b(?:you are now|you're now|act as|pretend (?:to be|you are)"
            r"|roleplay as|role-play as|imagine you are|behave as|respond as)\b"
            r"[^.\n]{0,70}\b(?:dan\b|do anything now|unrestricted|unfiltered"
            r"|uncensored|without (?:any )?(?:rules|restrictions|filters|limits"
            r"|guidelines)|no (?:rules|restrictions|filters|limits)|evil|jailbroken"
            r"|not (?:an? )?(?:ai|assistant|language model))\b"
        ),
    ),
    (
        "injection.jailbreak_marker",
        CATEGORY_PERSONA,
        "high",
        _rx(
            r"\b(?:do anything now|jailbreak|jailbroken|developer mode|god mode"
            r"|dan mode|no restrictions mode|unfiltered mode)\b"
        ),
    ),
    (
        "injection.boundary_spoof",
        CATEGORY_BOUNDARY,
        "high",
        _rx(
            r"-{3,}\s*(?:begin|end)[^\n]{0,60}-{3,}"
            r"|\bBEGIN\s+(?:SYSTEM|DEVELOPER|TRUSTED|UNTRUSTED)[^\n]{0,40}"
            r"|\bEND\s+(?:SYSTEM|DEVELOPER|TRUSTED|UNTRUSTED)[^\n]{0,40}"
        ),
    ),
    (
        "injection.fake_role_turn",
        CATEGORY_BOUNDARY,
        "high",
        _rx(
            r"(?m)^\s{0,3}(?:system|developer|assistant|tool|function)\s*:"
            r"|<\s*/?\s*(?:system|im_start|im_end|assistant|developer)\s*>"
            r"|\[\s*/?\s*(?:INST|SYSTEM)\s*\]"
            r"|#{2,}\s*(?:system|developer|instructions?)\b"
        ),
    ),
    (
        "injection.url_instruction",
        CATEGORY_URL,
        "medium",
        _rx(
            r"\b(?:visit|go to|open|navigate to|browse to|fetch|download|click"
            r"|follow this link)\b[^.\n]{0,30}(?:https?://|www\.)"
        ),
    ),
    (
        "injection.exfil_url",
        CATEGORY_URL,
        "high",
        _rx(
            r"\b(?:send|post|upload|forward|paste|submit|transmit|email|exfiltrate)\b"
            r"[^.\n]{0,45}(?:https?://|webhook|pastebin|ngrok\.io|requestbin"
            r"|burpcollaborator|transfer\.sh|0x0\.st)"
        ),
    ),
)

# Patterns whose whole matched span is removed when the remainder still carries
# a request (used by strip_directives) and replaced by a marker in neutralise().
_SPAN_RULES = tuple(
    pattern
    for rule, _category, _severity, pattern in _DIRECTIVES
    if rule
    in {
        "injection.override_phrase",
        "injection.new_instructions",
        "injection.exfil_request",
        "injection.url_instruction",
        "injection.exfil_url",
    }
)

# Patterns that poison the whole sentence: a persona switch or a fake role turn
# cannot be surgically removed without leaving nonsense behind.
_SEGMENT_RULES = tuple(
    pattern
    for rule, _category, _severity, pattern in _DIRECTIVES
    if rule
    in {
        "injection.persona_switch",
        "injection.jailbreak_marker",
        "injection.boundary_spoof",
        "injection.fake_role_turn",
    }
)

_ALL_RULES = tuple(pattern for _r, _c, _s, pattern in _DIRECTIVES)

# --- invisible characters and homoglyphs -----------------------------------
ZERO_WIDTH = "\u200b\u200c\u200d\u200e\u200f\u180e\u2060\u2061\u2062\u2063\u2064\ufeff"
_INVISIBLE = re.compile(f"[{re.escape(ZERO_WIDTH)}\u202a-\u202e\u2066-\u2069\U000E0000-\U000E007F]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

HOMOGLYPHS: dict[str, str] = {
    # Cyrillic look-alikes
    "а": "a", "в": "b", "г": "r", "е": "e", "ё": "e", "і": "i", "ї": "i",
    "ј": "j", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c",
    "т": "t", "у": "y", "х": "x", "ѕ": "s", "ԁ": "d", "һ": "h", "ӏ": "l",
    "А": "A", "В": "B", "Е": "E", "І": "I", "Ј": "J", "К": "K", "М": "M",
    "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    "Ѕ": "S", "Ԁ": "D", "Һ": "H", "Ӏ": "l",
    # Greek look-alikes
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o",
    "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "ω": "w", "γ": "y",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
}


def _homoglyph_hits(text: str) -> list[InjectionHit]:
    hits: list[InjectionHit] = []
    for match in _WORD.finditer(text):
        word = match.group(0)
        if word.isascii():
            continue
        if not any(char in HOMOGLYPHS for char in word):
            continue
        if not any(char.isascii() and char.isalpha() for char in word):
            continue
        hits.append(
            InjectionHit(
                "injection.homoglyph_word",
                CATEGORY_OBFUSCATION,
                _excerpt(word),
                "high",
            )
        )
    return hits


def _invisible_hits(text: str) -> list[InjectionHit]:
    hits: list[InjectionHit] = []
    invisible = _INVISIBLE.findall(text)
    if invisible:
        hits.append(
            InjectionHit(
                "injection.zero_width",
                CATEGORY_OBFUSCATION,
                f"{len(invisible)} invisible character(s)",
                "high",
            )
        )
    control = _CONTROL.findall(text)
    if control:
        hits.append(
            InjectionHit(
                "injection.control_chars",
                CATEGORY_CONTROL,
                f"{len(control)} control character(s)",
                "medium",
            )
        )
    return hits


def scan_injection(text: str | None) -> list[InjectionHit]:
    """Every injection-shaped span in ``text``, in a deterministic order.

    Directives are matched against a *folded* copy of the text (invisible
    characters removed, homoglyphs unmasked), so obfuscation does not hide the
    phrase. The obfuscation itself is still reported as its own hit.
    """
    if not text:
        return []
    body = text if isinstance(text, str) else str(text)
    folded = unmask_homoglyphs(_strip_invisibles(body))
    hits: list[InjectionHit] = []
    for rule, category, severity, pattern in _DIRECTIVES:
        for match in pattern.finditer(folded):
            hits.append(InjectionHit(rule, category, _excerpt(match.group(0)), severity))
            if len(hits) >= MAX_HITS:
                return hits
    hits.extend(_invisible_hits(body))
    hits.extend(_homoglyph_hits(body))
    return _dedupe(hits)[:MAX_HITS]


def _dedupe(hits: list[InjectionHit]) -> list[InjectionHit]:
    seen: set[tuple[str, str]] = set()
    ordered: list[InjectionHit] = []
    for hit in hits:
        key = (hit.rule, hit.excerpt.lower())
        if key in seen:
            continue
        seen.add(key)
        ordered.append(hit)
    return ordered


def has_injection(text: str | None) -> bool:
    return bool(scan_injection(text))


def injection_categories(hits: list[InjectionHit] | tuple[InjectionHit, ...]) -> list[str]:
    """Unique categories, sorted, for verdicts and audit rows."""
    return sorted({hit.category for hit in hits})


# --- neutralisation --------------------------------------------------------
def _strip_invisibles(text: str) -> str:
    return _CONTROL.sub("", _INVISIBLE.sub("", text))


def unmask_homoglyphs(text: str) -> str:
    """Fold the look-alike characters back to ASCII."""
    return "".join(HOMOGLYPHS.get(char, char) for char in text)


def neutralise(text: str | None) -> str:
    """A copy of ``text`` with directives defused and markers removed.

    Invisible characters are dropped, homoglyphs folded, spoofed boundary
    markers replaced, and every directive span replaced by ``[directive ...]``
    so the model sees text that cannot act as an instruction.
    """
    if not text:
        return text or ""
    body = _strip_invisibles(str(text))
    body = unmask_homoglyphs(body)
    body = body.replace(UNTRUSTED_BEGIN, "[boundary marker removed]")
    body = body.replace(UNTRUSTED_END, "[boundary marker removed]")
    for pattern in _SEGMENT_RULES:
        body = pattern.sub("[directive removed]", body)
    for pattern in _SPAN_RULES:
        body = pattern.sub("", body)
    body = re.sub(r"[ \t]{2,}", " ", body)
    return body.strip()


_CONNECTIVE = re.compile(r"(?i)^[\s,;:.\-]*(?:and|then|but|also|so|please|now)\b[\s,;:.\-]*")


def _tidy(fragment: str) -> str:
    text = re.sub(r"\s+", " ", fragment).strip()
    previous = None
    while previous != text:
        previous = text
        text = _CONNECTIVE.sub("", text).strip()
    return text.strip(" \t,;:-")


def _segments(text: str) -> list[str]:
    parts: list[str] = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line:
            continue
        parts.extend(piece for piece in re.split(r"(?<=[.!?;])\s+", line) if piece.strip())
    return parts or ([str(text).strip()] if str(text).strip() else [])


def strip_directives(text: str | None) -> tuple[str, list[str]]:
    """Split text into (legitimate remainder, removed directive segments).

    A segment that is entirely a directive is dropped. A segment that mixes a
    directive with a real request keeps the request: "Ignore previous
    instructions and tell me X" leaves "tell me X". Invisible characters are
    removed and homoglyphs folded first, so obfuscation cannot smuggle a
    directive into what the model sees.
    """
    body = unmask_homoglyphs(_strip_invisibles(str(text or "")))
    kept: list[str] = []
    removed: list[str] = []
    for segment in _segments(body):
        if any(pattern.search(segment) for pattern in _SEGMENT_RULES):
            removed.append(segment)
            continue
        cleaned = segment
        directive_found = False
        for pattern in _SPAN_RULES:
            if pattern.search(cleaned):
                cleaned = pattern.sub("", cleaned)
                directive_found = True
        if not directive_found:
            kept.append(segment)
            continue
        cleaned = _tidy(cleaned)
        if len(cleaned.split()) >= 3:
            kept.append(cleaned)
        else:
            removed.append(segment)
    return "\n".join(kept).strip(), removed
