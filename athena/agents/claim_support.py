"""Deterministic claim-to-evidence support checks.

The writer may paraphrase, but it may not turn a title, URL, navigation shell,
or a prior-release memory into a captured fact. This module compares the actual
sentences in the report with the full text of the evidence items, not with a
source title or a search-result snippet alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from athena.agents.routing import relevance

_TOKEN = re.compile(r"[a-z0-9][a-z0-9.+#_%/-]*", re.IGNORECASE)
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_SOURCE_ID = re.compile(r"\b(?:web|fetch|doc|calculate)-[a-f0-9]{6,}\b", re.I)
_URL = re.compile(r"https?://\S+", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "what", "which",
    "how", "why", "does", "did", "has", "have", "had", "are", "was", "were",
    "is", "be", "been", "being", "also", "but", "not", "only", "into", "than",
    "then", "when", "where", "who", "whom", "its", "their", "there", "these",
    "those", "they", "them", "can", "could", "would", "should", "may", "might",
    "will", "shall", "about", "more", "most", "other", "some", "such", "very",
    "answer", "report", "evidence", "source", "sources", "according", "using",
    "use", "used", "include", "includes", "including", "explain", "explains",
    "brief", "briefly", "key", "point", "points", "question", "result",
}
_QUALIFICATION = re.compile(
    r"\b(?:unverified|not verified|not established|cannot be determined|"
    r"no retrieved source|does not (?:state|define|contain|establish|support)|"
    r"insufficient|limitation|limitation:|remain(?:s)? unverified|unsupported|"
    r"evidence does not|not supported by the (?:retrieved|available) evidence|"
    r"could not be generated|writer failed|report could not be generated)\b",
    re.I,
)

# --- Claim-level checks ----------------------------------------------------
# The report may paraphrase, but it may not add precision the evidence does not
# have: patch numbers, dates, specifications, superlatives or a planned
# capability asserted as an existing one.
_SPECIFIC = re.compile(
    r"(?<![\w.])(?:\d+(?:[.,]\d+)*%?|\$\d[\d,.]*)(?![\w])"
)
_YEAR = re.compile(r"\b(?:1[5-9]\d{2}|20\d{2})s?\b")
_SUPERLATIVE = re.compile(
    r"\b(?:the\s+)?(?:first|only|largest|biggest|smallest|fastest|highest|lowest|"
    r"best|worst|most (?:efficient|powerful|common|important)|never|always|"
    r"unique|unparalleled)\b",
    re.I,
)
_HEDGE_WORD = (
    r"(?:about|approximately|approx\.?|roughly|around|nearly|almost|circa|"
    r"up to|as much as|at least|at most|no more than|over|more than|estimated|"
    r"some|several)"
)
_HEDGE_BEFORE_NUMBER = re.compile(
    rf"\b{_HEDGE_WORD}\s+([0-9][0-9.,]*)", re.I
)
_CLAIM_HEDGE = re.compile(rf"\b{_HEDGE_WORD}\b", re.I)
_PLANNED = re.compile(
    r"\b(?:planned|proposed|under development|in development|in testing|"
    r"prototype|test article|demonstrator|concept|scheduled|could be|would be)\b",
    re.I,
)
_IN_SERVICE = re.compile(
    r"\b(?:is used|are used|was used|were used|operational|in service|in flight|"
    r"already used|currently (?:used|flying|operational)|has been used)\b",
    re.I,
)
_ANAPHORA = re.compile(
    r"\b(?:it|its|they|their|them|this|that|these|those|the (?:system|engine|"
    r"thruster|vehicle|device|propellant|motor))\b",
    re.I,
)
_ADVISORY = "advisory"


def _normalise(token: str) -> str:
    token = token.lower().strip("._-/")
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("ses", "xes", "zes", "ches", "shes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is")) and len(token) > 3:
        return token[:-1]
    return token


def claim_tokens(text: str) -> set[str]:
    cleaned = _URL.sub(" ", _SOURCE_ID.sub(" ", text or ""))
    return {
        _normalise(token)
        for token in _TOKEN.findall(cleaned)
        if len(token) >= 3 and _normalise(token) not in _STOP
    }


def is_qualification(sentence: str) -> bool:
    return bool(_QUALIFICATION.search(sentence or ""))


def _numbers(text: str) -> set[str]:
    return {match.group(0) for match in _NUMBER.finditer(text or "")}


def support_score(claim: str, evidence_text: str) -> float:
    """Fraction of distinctive claim tokens found in one evidence body.

    Numbers/dates must all be present. A claim with fewer than three distinctive
    tokens is not treated as a factual claim by this conservative checker.
    """
    tokens = claim_tokens(claim)
    if len(tokens) < 3:
        return 1.0
    evidence = (evidence_text or "").lower()
    evidence_tokens = {
        _normalise(token) for token in _TOKEN.findall(evidence)
    }
    hits = sum(1 for token in tokens if token in evidence_tokens)
    score = hits / len(tokens)
    numbers = _numbers(claim)
    if numbers:
        missing = [number for number in numbers if number not in evidence]
        if missing:
            return 0.0
    return score


def best_support(claim: str, evidence_texts: list[str]) -> float:
    return max((support_score(claim, text) for text in evidence_texts), default=0.0)


def is_supported(claim: str, evidence_texts: list[str], threshold: float = 0.35) -> bool:
    if is_qualification(claim):
        return True
    return best_support(claim, evidence_texts) >= threshold


def unsupported_specifics(claim: str, evidence_text: str) -> tuple[str, ...]:
    """Numbers, versions and dates the claim states that the evidence does not."""
    evidence = (evidence_text or "").lower()
    missing: list[str] = []
    for match in _SPECIFIC.finditer(claim or ""):
        token = match.group(0).rstrip(".,")
        if token and token not in evidence:
            missing.append(token)
    for match in _YEAR.finditer(claim or ""):
        token = match.group(0)
        if token not in evidence:
            missing.append(token)
    return tuple(dict.fromkeys(missing))


def superlatives(claim: str, evidence_text: str) -> tuple[str, ...]:
    """Absolute or superlative wording the evidence never uses."""
    evidence = (evidence_text or "").lower()
    found: list[str] = []
    for match in _SUPERLATIVE.finditer(claim or ""):
        phrase = match.group(0).lower().strip()
        if phrase and phrase not in evidence:
            found.append(phrase)
    return tuple(dict.fromkeys(found))


def qualifier_dropped(claim: str, evidence_text: str) -> bool:
    """True when the claim repeats an approximate figure as if it were exact."""
    if _CLAIM_HEDGE.search(claim or ""):
        return False
    for match in _HEDGE_BEFORE_NUMBER.finditer(evidence_text or ""):
        number = match.group(1).rstrip(".,")
        if not number:
            continue
        if re.search(rf"(?<![\d.]){re.escape(number)}(?![\d])", claim or ""):
            return True
    return False


def capability_escalation(claim: str, evidence_text: str) -> bool:
    """True when a planned/prototype capability is asserted as existing."""
    claim_text = claim or ""
    evidence = evidence_text or ""
    if not _IN_SERVICE.search(claim_text):
        return False
    if _IN_SERVICE.search(evidence):
        return False
    return bool(_PLANNED.search(evidence))


def exceeds_evidence(claim: str, evidence_text: str) -> tuple[str, ...]:
    """Reasons the claim goes beyond what one evidence text states.

    Empty means the claim does not exceed the evidence; the caller still has to
    check that the claim is supported at all.
    """
    reasons: list[str] = []
    missing = unsupported_specifics(claim, evidence_text)
    if missing:
        reasons.append(
            "states " + ", ".join(missing) + " which the evidence does not"
        )
    absolute = superlatives(claim, evidence_text)
    if absolute:
        reasons.append(
            "claims " + ", ".join(absolute) + " which the evidence does not state"
        )
    if qualifier_dropped(claim, evidence_text):
        reasons.append("drops the source's qualifier from a figure")
    if capability_escalation(claim, evidence_text):
        reasons.append(
            "states a planned or proposed capability as an existing one"
        )
    return tuple(reasons)


def mentions_subject(text: str, question: str) -> bool:
    """True when a sentence is at least about the asked subject (or its subject)."""
    from athena.agents.routing import named_entities, subject_terms

    names = named_entities(question or "")
    terms = subject_terms(question or "")
    if not names and not terms:
        return True
    lowered = (text or "").lower()
    if any(name.lower() in lowered for name in names):
        return True
    if any(term in lowered for term in terms):
        return True
    return bool(_ANAPHORA.search(text or ""))


@dataclass(frozen=True)
class ClaimCheck:
    """One sentence of the report checked against the recorded evidence."""

    claim: str
    supported: bool
    score: float = 0.0
    supporting_refs: tuple[str, ...] = ()
    cited_refs: tuple[str, ...] = ()
    citation_mismatch: bool = False
    entity_relevant: bool = True
    exceeds: tuple[str, ...] = ()
    dropped_qualifier: bool = False
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return (
            self.supported
            and not self.exceeds
            and not self.dropped_qualifier
            and not self.citation_mismatch
            and self.entity_relevant
        )

    def describe(self) -> str:
        problems = list(self.exceeds)
        if self.dropped_qualifier:
            problems.append("drops the source's qualifier")
        if not self.supported:
            problems.append("no recorded source supports it")
        if self.citation_mismatch:
            problems.append("no cited source supports it")
        if not self.entity_relevant:
            problems.append("not about the asked subject")
        return "; ".join(problems) or "supported"


def validate_claims(
    text_or_sentences,
    evidence_items,
    question: str = "",
    citations=None,
    threshold: float = 0.35,
) -> tuple[ClaimCheck, ...]:
    """Check every report sentence: support, entity, citation and precision."""
    if isinstance(text_or_sentences, str):
        sentences = split_sentences(text_or_sentences)
    else:
        sentences = [str(item) for item in (text_or_sentences or [])]
    items = list(evidence_items or [])
    texts = [(getattr(item, "source_ref", ""), getattr(item, "content", "") or "")
             for item in items]
    cited = {getattr(citation, "source_ref", "") for citation in (citations or [])}
    cited_texts = [text for ref, text in texts if ref in cited]

    checks: list[ClaimCheck] = []
    for sentence in sentences:
        if not sentence.strip():
            continue
        if is_qualification(sentence):
            checks.append(ClaimCheck(claim=sentence, supported=True))
            continue
        scores = [(ref, support_score(sentence, text)) for ref, text in texts]
        supporting = tuple(
            dict.fromkeys(ref for ref, score in scores if score >= threshold and ref)
        )
        best = max((score for _ref, score in scores), default=0.0)
        supporting_texts = [
            text for _ref, text in texts if support_score(sentence, text) >= threshold
        ]
        reasons = [
            exceeds_evidence(sentence, text) for text in supporting_texts
        ]
        # A claim exceeds the evidence only when every source that supports it
        # says less than the claim does.
        exceeds = tuple(reasons[0]) if reasons and all(reasons) else ()
        dropped = bool(supporting_texts) and all(
            qualifier_dropped(sentence, text) for text in supporting_texts
        )
        citation_mismatch = bool(
            cited and supporting and not any(
                support_score(sentence, text) >= threshold for text in cited_texts
            )
        )
        checks.append(
            ClaimCheck(
                claim=sentence,
                supported=bool(supporting),
                score=round(best, 3),
                supporting_refs=supporting,
                cited_refs=tuple(sorted(cited)),
                citation_mismatch=citation_mismatch,
                entity_relevant=mentions_subject(sentence, question),
                exceeds=exceeds,
                dropped_qualifier=dropped,
            )
        )
    return tuple(checks)


def claim_problems(checks) -> tuple[str, ...]:
    """Short, deterministic descriptions of the claims that failed a check."""
    problems: list[str] = []
    for check in checks or ():
        if check.ok:
            continue
        problems.append(f"{check.claim.strip()[:160]} ({check.describe()})")
    return tuple(dict.fromkeys(problems))


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.split(text or "") if part.strip()]


def unsupported_sentences(
    text: str, evidence_texts: list[str], threshold: float = 0.35
) -> list[str]:
    return [
        sentence for sentence in split_sentences(text)
        if not is_supported(sentence, evidence_texts, threshold)
    ]


def _sentence_problems(sentence: str, evidence_texts: list[str], threshold: float) -> list[str]:
    """Why one sentence cannot ship: unsupported, or more precise than evidence."""
    if not is_supported(sentence, evidence_texts, threshold):
        return ["unsupported"]
    supporting = [
        text for text in evidence_texts
        if support_score(sentence, text) >= threshold
    ]
    problems: list[str] = []
    if supporting and all(qualifier_dropped(sentence, text) for text in supporting):
        problems.append("qualifier dropped")
    if supporting and all(
        unsupported_specifics(sentence, text) for text in supporting
    ):
        problems.append("exceeds the evidence")
    return problems


def advisory_problems(sentence: str, evidence_texts: list[str], threshold: float = 0.35) -> tuple[str, ...]:
    """Over-claims that are reported rather than silently deleted.

    A superlative ("the first", "the most efficient") or a planned capability
    asserted as existing changes the meaning of an answer without inventing a
    number, so it is surfaced to the critic and to the confidence calculation
    instead of being cut from the prose.
    """
    supporting = [
        text for text in evidence_texts
        if support_score(sentence, text) >= threshold
    ]
    if not supporting:
        return ()
    reasons: list[str] = []
    for text in supporting[:1]:
        absolute = superlatives(sentence, text)
        if absolute:
            reasons.append("claims " + ", ".join(absolute) + " without support")
        if capability_escalation(sentence, text):
            reasons.append("states a planned capability as existing")
    return tuple(dict.fromkeys(reasons))


def remove_unsupported(
    text: str, evidence_texts: list[str], threshold: float = 0.35
) -> tuple[str, list[str]]:
    """Remove unsupported or over-precise sentences, preserving qualifications."""
    kept: list[str] = []
    removed: list[str] = []
    for sentence in split_sentences(text):
        problems = _sentence_problems(sentence, evidence_texts, threshold)
        if problems:
            removed.append(sentence)
        else:
            kept.append(sentence)
    return " ".join(kept).strip(), removed


def best_excerpt(
    content: str, report_text: str, limit: int = 260
) -> str:
    """Choose the source passage that best matches the report for display."""
    text = " ".join((content or "").split())
    if not text:
        return ""
    report_tokens = claim_tokens(report_text)
    chunks = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|\n{2,}", content or "")
        if len(part.strip()) >= 40
    ]
    if not chunks:
        return text[:limit] + ("..." if len(text) > limit else "")
    best = min(
        chunks,
        key=lambda part: -len(report_tokens & claim_tokens(part)),
    )
    if len(report_tokens & claim_tokens(best)) == 0:
        best = max(chunks, key=len)
    best = " ".join(best.split())
    return best[:limit] + ("..." if len(best) > limit else "")


def evidence_texts_for_refs(items, refs: set[str]) -> list[str]:
    return [
        getattr(item, "content", "") or ""
        for item in items
        if getattr(item, "source_ref", "") in refs
    ]


def relevant_for_question(item, question: str) -> bool:
    return relevance(getattr(item, "content", ""), question).relevant
