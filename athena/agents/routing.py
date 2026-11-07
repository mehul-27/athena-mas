"""Deterministic source routing for research questions.

Prompts help, but they cannot be the only routing mechanism. This module decides
whether a question needs current external documentation, a supplied local
document, or general evidence. It also ensures the plan contains at least one
step for the appropriate source class.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

LOCAL_MARKERS = (
    "local document", "provided document", "attached document", "supplied document",
    "this document", "the document", "the handbook", "the pdf", "corpus",
    "according to the", "in the provided", "in the attached", "local file",
)
EXTERNAL_MARKERS = (
    "software", "programming", "language", "library", "framework", "package",
    "api", "sdk", "release", "version", "changelog", "documentation", "docs",
    "specification", "standard", "official", "latest", "current", "security",
    "cve", "python", "javascript", "rust", "java", "golang",
)
OFFICIAL_MARKERS = ("official", "primary source", "specification", "standard")
SECONDARY_HOSTS = (
    "geeksforgeeks.org", "medium.com", "dev.to", "w3schools.com",
    "tutorialspoint.com", "javatpoint.com", "stackoverflow.com", "reddit.com",
    "youtube.com", "linkedin.com", "quora.com", "blogspot.com", "wordpress.com",
    "towardsdatascience.com", "github.io",
    # Tertiary/enthusiast coverage is useful corroboration, not primary evidence.
    "wikipedia.org", "unpluggedperformance.com", "motortrend.com",
)
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "what", "which",
    "three", "four", "five", "give", "explain", "brief", "briefly", "each",
    "one", "two", "important", "key", "major", "about", "into", "should",
    "would", "could", "using", "source", "sources", "finding", "findings",
    "change", "changes", "practical", "impact", "official", "url",
}
_TERM = re.compile(r"[a-z0-9][a-z0-9.+#_-]{2,}", re.IGNORECASE)
_TOKEN = re.compile(r"[a-z0-9][a-z0-9.+#_]*", re.IGNORECASE)
_URL_TEXT = re.compile(r"https?://\S+", re.IGNORECASE)


def _without_urls(text: str) -> str:
    return _URL_TEXT.sub(" ", text or "")


_NAMED = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*\b")
_NAMED_STOP = {
    "The", "This", "That", "These", "Those", "What", "Which", "How", "Why",
    "When", "Where", "Who", "Explain", "Describe", "Compare", "Cite", "Fetch",
    "Read", "According", "Does", "Did", "Can", "Could", "Would", "Should",
    "Please", "Give", "Use", "Using", "From", "Into", "With", "For", "And",
    "Some", "Others", "Both", "Each", "All", "Reconcile", "Distinguish",
    "URL", "PDF", "API", "HTTP", "HTTPS", "AI", "RAG", "ID", "OK",
}
# These words describe the request, not the subject. Keeping them out of the
# subject set prevents a generic page containing "space" or "system" from
# looking like evidence about a specific technology.
_GENERIC_TERMS = {
    "does", "how", "why", "what", "which", "explain", "describe", "compare",
    "comparison", "working", "work", "behind", "principle", "operating",
    "operation", "role", "system", "systems", "propellant", "propellants",
    "advantage", "advantages", "disadvantage", "disadvantages", "application",
    "applications", "typical", "latest", "stable", "release", "released",
    "change", "changes", "changed", "space", "spacecraft", "control",
    "efficient", "efficiency", "process", "device", "use", "used", "uses",
    "they", "their", "them", "and", "the", "for", "with", "from", "that",
    "doe", "some", "reconcile", "describe", "accounts", "account", "sources",
    "others", "while", "answer", "question", "read", "summarise", "summarize",
    "summary", "brief", "introduced", "includes", "include", "according",
    "supplied", "cover", "covers", "its", "those", "other", "doe", "use",
    "using", "given", "provided", "please",
    # Request vocabulary around releases: "the latest version" is what is being
    # asked for, not what the subject is.
    "version", "versions", "newest", "patch", "point", "update", "updates",
}
_QUANTITATIVE = re.compile(
    r"(?<![a-z])(?:calculate|compute|how much|how many|numeric|numerical|"
    r"arithmetic|equation|formula|convert|rate|ratio|percentage|percent|"
    r"mass flow|specific impulse|thrust)(?![a-z])",
    re.IGNORECASE,
)
_FORECAST = re.compile(
    r"(?<![a-z])(?:forecast|forecasting|predict|prediction|project|projected|"
    r"projection|trend|extrapolat(?:e|ion)|future)(?![a-z])",
    re.IGNORECASE,
)
_MEASUREMENT = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:kg|g|mg|m/s|km/s|k|kelvin|pa|kpa|mpa|psi|bar|"
    r"n|kn|newtons?|s|sec|seconds?|%|percent|j|kj|w|kw)\b",
    re.IGNORECASE,
)
_ARITHMETIC = re.compile(r"\d\s*[+\-*/%^]\s*\d")
_CONCEPTUAL = re.compile(
    r"(?<![a-z])(?:how does|how do|how is|what is|what are|why does|why do|"
    r"working behind|explain|principle|concept|meaning)(?![a-z])",
    re.IGNORECASE,
)
_TECHNICAL = re.compile(
    r"(?<![a-z])(?:technical|specification|spec|architecture|mechanism|"
    r"operating principle|performance|design)(?![a-z])",
    re.IGNORECASE,
)
_COMPARISON = re.compile(
    r"(?<![a-z])(?:compare|comparison|versus|vs\.?|difference between|"
    r"better than)(?![a-z])",
    re.IGNORECASE,
)
_DETAILED = re.compile(
    r"(?<![a-z])(?:research|report|cite|citation|findings?|key changes?|"
    r"detailed|sources?)(?![a-z])",
    re.IGNORECASE,
)


def _has_marker(text: str, marker: str) -> bool:
    """Word-aware matching so 'rust' does not match the word 'thruster'."""
    pattern = rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z])"
    return re.search(pattern, text) is not None


@dataclass(frozen=True)
class RequestProfile:
    """A lightweight, deterministic read of what the user asked for."""

    kind: str
    allows_calculation: bool
    allows_forecast: bool
    guidance: str
    notes: tuple[str, ...] = ()


def ambiguity_notes(question: str) -> tuple[str, ...]:
    """Flag wording that materially changes the answer without naming the system."""
    text = (question or "").lower()
    if "spacex" in text and "nitrogen" in text and not re.search(
        r"(?<![a-z])(?:dragon|falcon|starship|roadster|raptor|draco|superdraco)(?![a-z])",
        text,
    ):
        return (
            "The question does not name a SpaceX vehicle or thruster system. "
            "Distinguish in-space cold-gas/RCS thrusters from the Tesla Roadster "
            "demonstrator and state which system each claim describes; do not "
            "present demonstrator figures as flight hardware.",
        )
    return ()


def request_profile(question: str) -> RequestProfile:
    """Classify the request without another model call.

    This intentionally recognises only explicit wording. A conceptual question
    is not turned into an arithmetic exercise merely because it contains a
    physical noun; calculation is allowed only when the user asks for one or
    supplies numeric quantities.
    """
    text = (question or "").lower()
    notes = ambiguity_notes(question)
    forecast = _FORECAST.search(text) is not None
    has_measurements = _MEASUREMENT.search(text) is not None
    quantitative_words = (
        _QUANTITATIVE.search(text) is not None
        or _ARITHMETIC.search(text) is not None
    )
    comparison = _COMPARISON.search(text) is not None
    conceptual = _CONCEPTUAL.search(text) is not None
    technical = _TECHNICAL.search(text) is not None
    detailed = _DETAILED.search(text) is not None

    if forecast:
        return RequestProfile(
            "quantitative",
            True,
            True,
            "Plan one bounded forecast only for the requested series and horizon. "
            "Do not add unrelated calculations or web research.",
            notes,
        )
    if quantitative_words or has_measurements:
        return RequestProfile(
            "quantitative",
            True,
            False,
            "Plan one calculation step with a stated purpose and expected output. "
            "Do not forecast unless the task explicitly asks for a forecast.",
            notes,
        )
    if comparison:
        return RequestProfile(
            "comparison",
            False,
            False,
            "Compare only the named systems or sources, using primary evidence. "
            "Add calculation only when the question explicitly asks for quantities.",
            notes,
        )
    if detailed:
        return RequestProfile(
            "detailed",
            False,
            False,
            "Investigate each requested finding with a distinct primary source. "
            "Do not add calculation or forecast steps unless requested.",
            notes,
        )
    if conceptual:
        return RequestProfile(
            "conceptual",
            False,
            False,
            "Explain the concept and mechanism. Do not plan calculation or "
            "forecast steps unless the question explicitly asks for a numeric "
            "result. Prefer primary/technical sources.",
            notes,
        )
    if technical:
        return RequestProfile(
            "technical",
            False,
            False,
            "Explain the technical mechanism or specification. Use primary sources. "
            "Do not add calculations unless numeric inputs or a numeric result "
            "are explicitly requested.",
            notes,
        )
    return RequestProfile(
        "general",
        False,
        False,
        "Choose the source class that can answer the request. Do not add a "
        "calculation or forecast worker without a numeric request.",
        notes,
    )


@dataclass(frozen=True)
class Route:
    kind: str  # local, external, general
    needs_web: bool
    needs_rag: bool
    requires_official: bool


def classify(question: str) -> Route:
    text = (question or "").lower()
    local = any(_has_marker(text, marker) for marker in LOCAL_MARKERS)
    external = any(_has_marker(text, marker) for marker in EXTERNAL_MARKERS)
    official = any(_has_marker(text, marker) for marker in OFFICIAL_MARKERS)
    if local and not external:
        return Route("local", False, True, official)
    if local and external:
        # An explicit local-document question remains local unless it also asks
        # for current/official external material.
        if any(_has_marker(text, marker) for marker in ("official", "latest", "current")):
            return Route("external", True, True, official)
        return Route("local", False, True, official)
    if external:
        return Route("external", True, False, official)
    # Conceptual/technical questions that do not name a current external
    # capability still need evidence, but they are not automatically "web"
    # questions. The researcher instructions handle that choice.
    return Route("general", False, False, official)


def source_guidance(question: str) -> str:
    route = classify(question)
    profile = request_profile(question)
    if route.kind == "general" and profile.kind in {
        "conceptual", "technical", "comparison", "detailed"
    }:
        return (
            "This is a conceptual/technical question. Start with a general "
            "technical `web_search` query that does not name an organization or "
            "vehicle, then `fetch_page` the relevant HTML pages before making "
            "claims. Use a later query for organization-specific confirmation if "
            "budget remains. Use local RAG only when the corpus is plausibly "
            "relevant. A snippet alone is not sufficient evidence."
        )
    if route.kind == "local":
        return (
            "This question is about supplied local documents. Use `rag_search` "
            "for the relevant document sections; use the web only if the local "
            "corpus cannot answer. Do not substitute a search snippet for the "
            "document text."
        )
    if route.kind == "external":
        return (
            "This is a current/technical question. Start with `web_search` for "
            "official documentation, release notes, standards, or primary "
            "specifications, then use `fetch_page` on the relevant results. Use "
            "`rag_search` only if it is plausibly relevant or the question also "
            "refers to a local document. A search snippet alone is not sufficient "
            "evidence for a specific claim."
        )
    return (
        "Choose the source class that can actually answer the question. Use local "
        "RAG only when the corpus is relevant, and fetch primary pages for web "
        "claims; a snippet alone is not sufficient."
    )


def _normalise_token(token: str) -> str:
    token = token.lower().strip("._-")
    # A deliberately small stemmer: enough to match propellant/propellants,
    # thruster/thrusters and release/releases without translating unrelated words.
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("ses", "xes", "zes", "ches", "shes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is")) and len(token) > 3:
        return token[:-1]
    return token


def _tokens(text: str) -> set[str]:
    return {
        _normalise_token(token)
        for token in _TOKEN.findall((text or "").replace("-", " "))
        if len(token) >= 2
    }


def named_entities(question: str) -> list[str]:
    """Proper names likely to identify a subject, not sentence-initial prose.

    ``Some`` and ``Reconcile`` at the start of a sentence are not entities.
    Camel-case/acronym names are accepted anywhere. A hyphenated technical term
    (``Hall-effect``) is left to the subject-term path instead.
    """
    text = _without_urls(question or "")
    found: list[str] = []
    for match in _NAMED.finditer(text):
        word = re.sub(r"'s$", "", match.group(0))
        if "-" in word and word.split("-", 1)[1].islower():
            # Hall-effect is a technical compound, not a proper name.
            continue
        if word in _NAMED_STOP or word.lower() in {item.lower() for item in found}:
            continue
        after = text[match.end():match.end() + 1]
        if after == "-" and match.end() + 1 < len(text) and text[match.end() + 1].islower():
            continue
        before = text[:match.start()]
        sentence_start = not before.strip() or bool(
            re.search(r"(?:^|[.!?])\s*$", before)
        )
        internal_cap = any(char.isupper() for char in word[1:])
        acronym = word.isupper() and len(word) > 1
        if sentence_start and not internal_cap and not acronym:
            continue
        found.append(word)
    return found


def subject_terms(question: str) -> set[str]:
    """Technical subject terms, excluding request words and named entities."""
    terms = set()
    named = {item.lower() for item in named_entities(question)}
    for token in _TERM.findall(_without_urls(question).replace("-", " ")):
        normalised = _normalise_token(token)
        if (
            len(normalised) < 3
            or normalised in _STOPWORDS
            or normalised in _GENERIC_TERMS
            or normalised in {_normalise_token(item) for item in named}
        ):
            continue
        terms.add(normalised)
    return terms


def question_terms(question: str) -> set[str]:
    """Backward-compatible question vocabulary for callers that need terms."""
    return set(subject_terms(question)) | {
        _normalise_token(item) for item in named_entities(question)
    }


@dataclass(frozen=True)
class Relevance:
    """A conservative read of whether text addresses the asked subject."""

    subject_specific: bool
    background: bool
    matched_terms: tuple[str, ...]
    missing_terms: tuple[str, ...]
    matched_names: tuple[str, ...]
    missing_names: tuple[str, ...]
    score: float
    name_collision: bool = False

    @property
    def relevant(self) -> bool:
        return self.subject_specific or self.background


def relevance(text: str, question: str) -> Relevance:
    """Score subject and background relevance without a model call.

    Subject-specific requires every named entity and at least two subject terms
    (or one when the subject is a single term). Background requires two
    substantive subject terms and is usable only for a qualified explanation.
    One incidental keyword is never enough.
    """
    lowered = (text or "").lower()
    corpus_tokens = _tokens(text)
    terms = sorted(subject_terms(question))
    names = named_entities(question)
    matched_terms = tuple(term for term in terms if term in corpus_tokens)
    missing_terms = tuple(term for term in terms if term not in corpus_tokens)
    matched_names = tuple(name for name in names if name.lower() in lowered)
    missing_names = tuple(name for name in names if name.lower() not in lowered)
    if not terms and not names:
        # The question names no subject vocabulary (for example, it only gives
        # an explicit URL). Any substantive page is relevant, but it is not
        # evidence about an unstated subject.
        return Relevance(
            subject_specific=True,
            background=True,
            matched_terms=(),
            missing_terms=(),
            matched_names=(),
            missing_names=(),
            score=1.0,
        )
    required = max(1, min(2, len(terms)))
    if names:
        # Once every named subject is present, one supporting technical term is
        # enough; the name is the distinguishing signal.
        required = 1
    terms_ok = not terms or len(matched_terms) >= required
    names_ok = not missing_names
    subject_specific = bool(terms_ok and names_ok and (terms or names))
    background = bool(
        len(terms) >= 2
        and len(matched_terms) >= 2
        and not subject_specific
    )
    score = (
        (len(matched_terms) / len(terms)) if terms else (1.0 if names_ok else 0.0)
    )
    if names:
        score = min(score, len(matched_names) / len(names))
    return Relevance(
        subject_specific=subject_specific,
        background=background,
        matched_terms=matched_terms,
        missing_terms=missing_terms,
        matched_names=matched_names,
        missing_names=missing_names,
        score=round(score, 3),
        name_collision=bool(names and matched_names and missing_names),
    )


def is_relevant(text: str, question: str) -> bool:
    return relevance(text, question).relevant


def is_primary_source(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return not any(host == bad or host.endswith("." + bad) for bad in SECONDARY_HOSTS)


def ensure_step_texts(
    steps: list[str],
    question: str,
    enabled_tools: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Guarantee that the plan contains one step for the required source class.

    ``enabled_tools`` is enforced here as well: a required source step is only
    inserted when the run can actually execute it. The missing capability is
    reported separately by ``prepare_plan`` rather than being planned anyway.
    """
    from athena.tools import registry

    route = classify(question)
    profile = request_profile(question)
    selected = registry.normalise_selection(enabled_tools)
    texts = [str(step) for step in steps]
    needs_web = route.needs_web or (
        route.kind == "general"
        and profile.kind in {"conceptual", "technical", "comparison", "detailed"}
    )
    if route.kind == "external":
        # The planner often adds a generic RAG step to every plan. Without an
        # explicit local-corpus requirement, that spends a research stage on an
        # unrelated corpus and adds no citable evidence.
        texts = [
            text for text in texts
            if not text.lower().startswith("rag_researcher:")
        ]
    web_ok = registry.role_enabled("web_researcher", selected)
    rag_ok = registry.role_enabled("rag_researcher", selected)
    if needs_web and web_ok and not any(
        "web_researcher:" in text.lower() for text in texts
    ):
        texts.insert(0, "web_researcher: Search for official primary documentation, "
                        "release notes, or specifications, then fetch the relevant pages.")
    if route.needs_rag and rag_ok and not any(
        "rag_researcher:" in text.lower() for text in texts
    ):
        texts.insert(0, "rag_researcher: Search the supplied local corpus and record "
                        "the relevant passages.")
    return texts[:4]


def preferred_roles(question: str, available) -> list[str]:
    """Enabled research roles that fit the request, in priority order."""
    roles = list(available)
    route = classify(question)
    profile = request_profile(question)
    preferred: list[str] = []
    if route.kind == "local":
        preferred.append("rag_researcher")
    elif route.needs_web or profile.kind in {
        "conceptual", "technical", "comparison", "detailed"
    }:
        preferred.append("web_researcher")
    elif profile.kind == "quantitative":
        preferred.append("calc_researcher")
    if route.needs_rag:
        preferred.append("rag_researcher")
    if not preferred:
        preferred = ["web_researcher", "rag_researcher"]
    return [role for role in preferred if role in roles]


# --- Competing accounts ----------------------------------------------------
# A question can ask the run to reconcile two incompatible accounts ("some
# sources say X, while others say Y"). Neither side may be assumed: the answer
# has to be built from real evidence for each account, and when only one side
# was retrieved the report must say so instead of inventing a conflict.

_CONTRAST_MARKER = re.compile(
    r"\b(?:while|whereas|but|yet|although|though)\b", re.IGNORECASE
)
_MULTI_ACCOUNT = re.compile(
    r"\b(?:some|others?|other sources?|another|several|many|both|two)\b", re.IGNORECASE
)
_ACCOUNT_REQUEST = re.compile(
    r"\b(?:conflict\w*|differ\w*|disagree\w*|disput\w*|compet\w*|reconcil\w*|"
    r"accounts?|contradict\w*|opposite|versions? of)\b",
    re.IGNORECASE,
)
# Words that describe the request or the act of reporting, not the account.
_ACCOUNT_FILLER = {
    "some", "others", "other", "another", "several", "many", "both", "two", "one",
    "sources", "source", "describe", "describes", "described", "say", "says", "said",
    "state", "states", "stated", "claim", "claims", "called", "call", "calls",
    "treat", "treats", "reported", "reports", "while", "whereas", "although",
    "though", "them", "they", "their", "these", "those", "this", "that", "which",
    "with", "from", "into", "about", "uses", "use", "used", "using", "thruster",
    "thrusters", "engine", "engines", "motor", "motors", "spacecraft", "system",
    "systems", "vehicle", "vehicles", "according", "read", "fetch", "cited",
}
# A term is evidence for an account when any of its aliases appears.
_ACCOUNT_SYNONYMS = {
    "hypergolic": (
        "hypergolic", "hydrazine", "monomethylhydrazine", "mmh", "udmh",
        "nitrogen tetroxide", "n2o4", "dinitrogen tetroxide",
    ),
    "hydrazine": ("hydrazine", "monomethylhydrazine", "hypergolic", "mmh"),
    "monomethylhydrazine": ("monomethylhydrazine", "hydrazine", "mmh", "hypergolic"),
    "cold": ("cold gas", "cold-gas", "coldgas"),
    "nitrogen": ("nitrogen", "cold gas", "n2"),
    "electric": ("electric propulsion", "ion thruster", "ion engine", "hall-effect"),
    "solid": ("solid propellant", "solid rocket", "solid motor"),
}


@dataclass(frozen=True)
class Account:
    """One competing account a question asks about."""

    label: str
    terms: tuple[str, ...]
    clause: str = ""

    @property
    def markers(self) -> tuple[str, ...]:
        found: list[str] = []
        for term in self.terms:
            found.extend(_ACCOUNT_SYNONYMS.get(term, (term,)))
        return tuple(dict.fromkeys(found))

    def supported_by(self, text: str) -> bool:
        """True when the text states at least half of this account's terms."""
        lowered = (text or "").lower()
        hits = sum(1 for marker in self.markers if marker in lowered)
        needed = max(1, (len(self.markers) + 1) // 2)
        return hits >= needed


def _account_terms(clause: str, shared: set[str], names: set[str]) -> list[str]:
    terms: list[str] = []
    for token in _TOKEN.findall((clause or "").replace("-", " ")):
        normalised = _normalise_token(token)
        if (
            len(normalised) < 3
            or normalised in _ACCOUNT_FILLER
            or normalised in _GENERIC_TERMS
            or normalised in _STOPWORDS
            or normalised in shared
            or normalised in names
            or normalised in terms
        ):
            continue
        terms.append(normalised)
    return terms


def contrasting_accounts(question: str) -> tuple[Account, ...]:
    """The two accounts a question asks to reconcile, when it asks for two.

    Deterministic and deliberately narrow: it needs an explicit contrast plus
    plural reporting ("some ... while others ...") or reconciliation wording.
    """
    text = _without_urls(question or "")
    if not text.strip():
        return ()
    requested = bool(_ACCOUNT_REQUEST.search(text)) or bool(
        _MULTI_ACCOUNT.search(text) and _CONTRAST_MARKER.search(text)
    )
    if not requested:
        return ()
    match = _CONTRAST_MARKER.search(text)
    if match is None:
        return ()
    left, right = text[: match.start()], text[match.end():]
    names = {item.lower() for item in named_entities(question)}
    left_terms = _account_terms(left, set(), names)
    right_terms = _account_terms(right, set(), names)
    shared = {term for term in left_terms if term in right_terms}
    left_terms = [term for term in left_terms if term not in shared]
    right_terms = [term for term in right_terms if term not in shared]
    if not left_terms or not right_terms:
        return ()
    return (
        Account(label=" ".join(left_terms), terms=tuple(left_terms), clause=left.strip()),
        Account(label=" ".join(right_terms), terms=tuple(right_terms), clause=right.strip()),
    )


def critique_needs_research(issues: list[str]) -> bool:
    """True when the critic's issues point at evidence, not just prose."""
    markers = (
        "evidence", "source", "citation", "retriev", "fetch", "missing",
        "unsupported", "not supported", "not traceable", "irrelevant", "gap",
        "primary", "does not cover", "no support", "lacks support",
    )
    text = " ".join(str(issue).lower() for issue in (issues or []))
    return any(marker in text for marker in markers)
