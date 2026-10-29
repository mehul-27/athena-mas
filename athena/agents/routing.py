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
)
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "what", "which",
    "three", "four", "five", "give", "explain", "brief", "briefly", "each",
    "one", "two", "important", "key", "major", "about", "into", "should",
    "would", "could", "using", "source", "sources", "finding", "findings",
    "change", "changes", "practical", "impact", "official", "url",
}
_TERM = re.compile(r"[a-z0-9][a-z0-9.+#_-]{2,}", re.IGNORECASE)


@dataclass(frozen=True)
class Route:
    kind: str  # local, external, general
    needs_web: bool
    needs_rag: bool
    requires_official: bool


def classify(question: str) -> Route:
    text = (question or "").lower()
    local = any(marker in text for marker in LOCAL_MARKERS)
    external = any(marker in text for marker in EXTERNAL_MARKERS)
    official = any(marker in text for marker in OFFICIAL_MARKERS)
    if local and not external:
        return Route("local", False, True, official)
    if local and external:
        # An explicit local-document question remains local unless it also asks
        # for current/official external material.
        if any(marker in text for marker in ("official", "latest", "current")):
            return Route("external", True, True, official)
        return Route("local", False, True, official)
    if external:
        return Route("external", True, False, official)
    return Route("general", False, False, official)


def source_guidance(question: str) -> str:
    route = classify(question)
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


def question_terms(question: str) -> set[str]:
    terms = set()
    for token in _TERM.findall((question or "").lower()):
        if token in _STOPWORDS or len(token) < 3:
            continue
        terms.add(token)
    return terms


def is_relevant(text: str, question: str) -> bool:
    terms = question_terms(question)
    if not terms:
        return True
    lowered = (text or "").lower()
    return any(term in lowered for term in terms)


def is_primary_source(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return not any(host == bad or host.endswith("." + bad) for bad in SECONDARY_HOSTS)


def ensure_step_texts(steps: list[str], question: str) -> list[str]:
    """Guarantee that the plan contains one step for the required source class."""
    route = classify(question)
    texts = [str(step) for step in steps]
    if route.needs_web and not any("web_researcher:" in text.lower() for text in texts):
        texts.insert(0, "web_researcher: Search for official primary documentation, "
                        "release notes, or specifications, then fetch the relevant pages.")
    if route.needs_rag and not any("rag_researcher:" in text.lower() for text in texts):
        texts.insert(0, "rag_researcher: Search the supplied local corpus and record "
                        "the relevant passages.")
    return texts[:4]


def critique_needs_research(issues: list[str]) -> bool:
    """True when the critic's issues point at evidence, not just prose."""
    markers = (
        "evidence", "source", "citation", "retriev", "fetch", "missing",
        "unsupported", "not supported", "not traceable", "irrelevant", "gap",
        "primary", "does not cover", "no support", "lacks support",
    )
    text = " ".join(str(issue).lower() for issue in (issues or []))
    return any(marker in text for marker in markers)
