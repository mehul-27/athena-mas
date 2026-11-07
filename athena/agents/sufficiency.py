"""Deterministic evidence-sufficiency assessment for fixed research requirements.

This is deliberately conservative: snippets, RAG chunks from an unrelated
corpus, failed fetches, duplicate URLs, and secondary pages do not count as
independent primary-source support. The writer is blocked from claiming a
complete answer when this gate fails; the critic can still request recovery.

The assessment also knows which tools the user enabled. A missing capability is
reported as an unmet requirement rather than being treated as a failed search
or, worse, as a success.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from athena.agents.requirements import required_findings
from athena.agents.routing import (
    classify,
    contrasting_accounts,
    is_primary_source,
    named_entities,
    relevance,
    request_profile,
    subject_terms,
)
from athena.tools import registry

MIN_CONTENT_CHARS = 120
MIN_FETCHED_CHARS = 40
MIN_SUBSTANTIVE_CHARS = 160
MIN_RAG_CHARS = 40
# Confidence ceilings. A complete, subject-specific, primary-sourced answer may
# reach 0.8; anything less justified is capped below it.
CEILING_COMPLETE = 0.8
CEILING_SUBJECT = 0.6
CEILING_QUALIFIED = 0.5
CEILING_BACKGROUND = 0.5
CEILING_UNMET_REQUIREMENT = 0.45
CEILING_UNRESOLVED_CONFLICT = 0.45
CEILING_MISSING_ASPECT = 0.6
CEILING_NONE = 0.3
_URL = re.compile(r"https?://[^\s<>()\[\]{}\"']+")

_LATEST_REQUEST = re.compile(
    r"(?<![a-z])(?:latest|current|newest|most recent|stable)(?![a-z])", re.IGNORECASE
)
_VERSION_REQUEST = re.compile(
    r"(?<![a-z])(?:version|release|patch|point release|update)(?![a-z])", re.IGNORECASE
)
_VERSION_TRIPLE = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])")


@dataclass(frozen=True)
class Conflict:
    """One requested pair of competing accounts and what the evidence covers."""

    aspect: str
    accounts: tuple[str, ...]
    supported_accounts: tuple[str, ...] = ()
    missing_accounts: tuple[str, ...] = ()
    supporting_refs: tuple[str, ...] = ()
    genuine: bool = False
    note: str = ""

    @property
    def unresolved(self) -> bool:
        return bool(self.missing_accounts) or not self.genuine


@dataclass(frozen=True)
class Assessment:
    ok: bool
    route: str
    required: int
    supported: int
    distinct_sources: int
    primary_sources: int
    issues: tuple[str, ...]
    limitations: tuple[str, ...] = ()
    search_only: bool = False
    missing_tools: tuple[str, ...] = ()
    required_urls: tuple[str, ...] = ()
    subject_specific: bool = False
    background: bool = False
    missing_subjects: tuple[str, ...] = ()
    supported_aspects: tuple[str, ...] = ()
    missing_aspects: tuple[str, ...] = ()
    qualified_possible: bool = False
    answer_as_asked: bool = False
    # What the user asked for, and where the evidence sits against it.
    subject: str = ""
    requested_subjects: tuple[str, ...] = ()
    requested_subtopics: tuple[str, ...] = ()
    subject_source_refs: tuple[str, ...] = ()
    background_source_refs: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    evidence_quality: str = "none"
    ceiling: float = CEILING_NONE

    @property
    def confidence_ceiling(self) -> float:
        """The most confidence the evidence can justify."""
        return self.ceiling

    @property
    def unresolved_conflicts(self) -> tuple[Conflict, ...]:
        return tuple(item for item in self.conflicts if item.unresolved)


def explicit_urls(question: str) -> tuple[str, ...]:
    """Absolute URLs the user put in the question, cleaned of punctuation."""
    found: list[str] = []
    for match in _URL.finditer(question or ""):
        url = match.group(0).rstrip(".,;:!?)]}")
        if url and url not in found:
            found.append(url)
    return tuple(found)


def _url_key(url: str) -> tuple[str, str, str]:
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return ("", "", "")
    host = (parsed.hostname or "").lower().rstrip(".")
    path = (parsed.path or "/").rstrip("/") or "/"
    return (host, path, parsed.query or "")


def same_url(left: str, right: str) -> bool:
    return _url_key(left) == _url_key(right)


def named_terms(question: str) -> list[str]:
    """Backward-compatible name helper; the real extractor lives in routing."""
    return named_entities(question)


def requested_aspects(question: str) -> tuple[str, ...]:
    """Explicit answer parts that can be checked deterministically."""
    text = (question or "").lower()
    aspects: list[str] = []
    checks = (
        ("mechanism", ("how does", "how do", "work", "working", "operate",
                       "principle", "mechanism")),
        ("efficiency", ("efficient", "efficiency", "specific impulse", "isp")),
        ("propellant", ("propellant", "propellants", "fuel", "oxidizer",
                        "nitrogen", "hydrazine", "monomethylhydrazine")),
        ("role", ("role", "attitude", "control", "maneuver", "manoeuvre")),
        ("advantages", ("advantage", "advantages", "benefit", "benefits")),
        ("disadvantages", ("disadvantage", "disadvantages", "limitation",
                           "limitations", "trade-off", "tradeoff")),
        ("applications", ("application", "applications", "typical use",
                          "used for")),
        ("release", ("latest", "stable", "release", "version")),
        ("changes", ("what changed", "what's new", "whats new", "changes",
                     "new features")),
        ("definition", ("what does", "what is", "define", "definition",
                        "cover", "covers")),
        ("activities", ("activities", "elements", "includes", "include")),
    )
    for name, markers in checks:
        if any(marker in text for marker in markers):
            aspects.append(name)
    return tuple(dict.fromkeys(aspects))


_ASPECT_MARKERS = {
    "mechanism": ("work", "operat", "principle", "mechanism", "expand",
                  "nozzle", "valve", "field", "accelerat"),
    "efficiency": ("efficien", "specific impulse", "isp", "thrust", "exhaust"),
    "propellant": ("propellant", "fuel", "oxidizer", "nitrogen", "hydrazine",
                   "nitrous", "monomethylhydrazine", "cold gas"),
    "role": ("role", "attitude", "control", "maneuver", "manoeuvre",
             "station-keeping", "rcs"),
    "advantages": ("advantage", "benefit", "simple", "reliable", "lightweight"),
    "disadvantages": ("disadvantage", "limitation", "lower", "low thrust",
                      "trade-off", "tradeoff"),
    "applications": ("application", "used for", "attitude control",
                     "station-keeping", "proximity", "orbit"),
    "release": ("release", "stable", "version", "python 3."),
    "changes": ("new", "change", "changed", "feature", "added", "improved",
                "what's new"),
    "definition": ("configuration management", "defined as", "covers",
                   "is a", "is the", "is an", "was the", "are the", "refers to"),
    "activities": ("element", "activit", "audit", "baseline", "identify",
                   "planning", "change control", "status accounting"),
}


def _supported_aspects(question: str, corpus: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    lowered = (corpus or "").lower()
    supported = []
    missing = []
    for aspect in requested_aspects(question):
        markers = _ASPECT_MARKERS.get(aspect, ())
        if any(marker in lowered for marker in markers):
            supported.append(aspect)
        else:
            missing.append(aspect)
    return tuple(supported), tuple(missing)


def release_requirements(question: str, corpus: str) -> tuple[str, ...]:
    """Requested facts a complete answer must establish, but which it may not.

    "The latest stable release" is not answered by a major/minor line: the
    exact patch version has to be stated by the evidence. When it is not, the
    requirement is listed and the confidence ceiling drops.
    """
    text = question or ""
    if not (_LATEST_REQUEST.search(text) and _VERSION_REQUEST.search(text)):
        return ()
    if _VERSION_TRIPLE.search(corpus or ""):
        return ()
    return (
        "the exact latest patch version (x.y.z) of the current stable release, "
        "stated by a retrieved primary source",
    )


def conflicts_for(question: str, items) -> tuple[Conflict, ...]:
    """Evidence coverage for each pair of competing accounts the user named."""
    accounts = contrasting_accounts(question)
    if len(accounts) < 2:
        return ()
    corpus = " ".join(
        (getattr(item, "content", "") or "")
        for item in (items or [])
    )
    if not corpus.strip():
        empty = Conflict(
            aspect="competing accounts",
            accounts=tuple(account.label for account in accounts),
            note="No evidence was retrieved for either competing account.",
        )
        return (empty,)

    supported: list[str] = []
    missing: list[str] = []
    refs: dict[str, set[str]] = {account.label: set() for account in accounts}
    for account in accounts:
        hits = [
            item
            for item in (items or [])
            if account.supported_by(getattr(item, "content", "") or "")
            and _substantive(getattr(item, "content", "") or "")
        ]
        if hits:
            supported.append(account.label)
            refs[account.label] = {
                getattr(item, "source_ref", "") for item in hits
            }
        else:
            missing.append(account.label)

    genuine = len(supported) >= 2 and len(
        {ref for label in supported for ref in refs.get(label, set())}
    ) >= 2
    if genuine:
        note = (
            "Retrieved sources state both competing accounts ("
            + " vs ".join(supported)
            + "); report the conflict and attribute each account to its source."
        )
    elif supported:
        note = (
            "Only the " + ", ".join(supported) + " account is supported by a "
            "retrieved source; no genuinely conflicting credible source pair was "
            "retrieved, so the report must not invent a conflict."
        )
    else:
        note = (
            "No retrieved source supports either competing account ("
            + " vs ".join(account.label for account in accounts)
            + "); the conflict is unresolved."
        )
    all_refs: list[str] = []
    for label in supported:
        all_refs.extend(sorted(refs.get(label, set())))
    return (
        Conflict(
            aspect="competing accounts",
            accounts=tuple(account.label for account in accounts),
            supported_accounts=tuple(supported),
            missing_accounts=tuple(missing),
            supporting_refs=tuple(dict.fromkeys(all_refs)),
            genuine=genuine,
            note=note,
        ),
    )


def evidence_quality(primary_sources: int, subject_specific: bool, background: bool) -> str:
    if primary_sources:
        return "primary"
    if subject_specific:
        return "secondary"
    if background:
        return "background"
    return "none"


def confidence_ceiling(
    *,
    answer_as_asked: bool,
    subject_specific: bool,
    qualified_possible: bool,
    quality: str,
    missing_requirements: tuple[str, ...] = (),
    conflicts: tuple[Conflict, ...] = (),
    missing_aspects: tuple[str, ...] = (),
) -> float:
    """The most confidence the evidence can justify, as one number."""
    if answer_as_asked:
        ceiling = CEILING_COMPLETE
    elif subject_specific:
        ceiling = CEILING_SUBJECT
    elif qualified_possible:
        ceiling = CEILING_QUALIFIED
    else:
        ceiling = CEILING_NONE
    if quality == "background":
        ceiling = min(ceiling, CEILING_BACKGROUND)
    if quality == "none":
        ceiling = min(ceiling, CEILING_NONE)
    if not subject_specific:
        ceiling = min(ceiling, CEILING_QUALIFIED)
    if missing_aspects:
        ceiling = min(ceiling, CEILING_MISSING_ASPECT)
    if missing_requirements:
        ceiling = min(ceiling, CEILING_UNMET_REQUIREMENT)
    if any(item.unresolved for item in conflicts):
        ceiling = min(ceiling, CEILING_UNRESOLVED_CONFLICT)
    return round(ceiling, 3)


def _tool_failure_details(tool_report: dict | None, tool: str) -> list[str]:
    details = []
    for call in (tool_report or {}).get("calls") or []:
        if call.get("tool") != tool or call.get("ok"):
            continue
        if call.get("blocked_by"):
            continue
        detail = str(call.get("error") or call.get("result_status") or "failed").strip()
        text = f"{tool}: {detail}" if detail else tool
        if text not in details:
            details.append(text)
    if not details and tool in set((tool_report or {}).get("failed") or []):
        details.append(tool + " failed")
    return details


def _enabled(enabled_tools) -> set[str]:
    return set(registry.enabled_ids(registry.normalise_selection(enabled_tools)))


def _substantive(text: str) -> bool:
    # Short stubs and menu-only pages are not evidence. Relevance is checked
    # separately; length alone is not treated as topical relevance.
    return len(" ".join((text or "").split())) >= MIN_SUBSTANTIVE_CHARS


def assess(
    question: str,
    items,
    enabled_tools=None,
    tool_report: dict | None = None,
) -> Assessment:
    route = classify(question)
    required = required_findings(question)
    allowed = _enabled(enabled_tools)
    urls = explicit_urls(question)
    failed_tools = set((tool_report or {}).get("failed") or [])

    citable = [
        item for item in (items or [])
        if getattr(item, "source_type", "") in {"rag", "web", "calculation", "forecast"}
    ]
    relevance_by_id = {
        getattr(item, "id", ""): relevance(getattr(item, "content", ""), question)
        for item in citable
    }

    fetched_pages = [
        item for item in citable
        if getattr(item, "source_type", "") == "web"
        and str(getattr(item, "id", "")).startswith("fetch-")
        and str(getattr(item, "source_ref", "")).startswith(("http://", "https://"))
        and len((getattr(item, "content", "") or "").strip()) >= MIN_FETCHED_CHARS
        and _substantive(getattr(item, "content", ""))
    ]
    fetched_specific = [
        item for item in fetched_pages
        if relevance_by_id[item.id].subject_specific
    ]
    fetched_background = [
        item for item in fetched_pages
        if relevance_by_id[item.id].background
        and not relevance_by_id[item.id].subject_specific
    ]
    snippets = [
        item for item in citable
        if getattr(item, "source_type", "") == "web"
        and str(getattr(item, "id", "")).startswith("web-")
        and relevance_by_id[item.id].relevant
    ]
    rag_pages = [
        item for item in citable
        if getattr(item, "source_type", "") == "rag"
        and len((getattr(item, "content", "") or "").strip()) >= MIN_RAG_CHARS
    ]
    rag_specific = [
        item for item in rag_pages
        if relevance_by_id[item.id].subject_specific
    ]
    rag_background = [
        item for item in rag_pages
        if relevance_by_id[item.id].background
        and not relevance_by_id[item.id].subject_specific
    ]

    subject_pool = list(fetched_specific) + list(rag_specific)
    background_pool = list(fetched_background) + list(rag_background)
    if route.kind == "local":
        evidence_pool = list(rag_specific)
    elif route.kind == "external":
        evidence_pool = list(fetched_specific)
    else:
        evidence_pool = list(subject_pool) or list(background_pool)
    primary = [item for item in subject_pool if is_primary_source(item.source_ref)]
    if route.requires_official:
        evidence_pool = primary

    distinct = {}
    for item in evidence_pool:
        distinct.setdefault(item.source_ref, item)
    supported = len(distinct)
    subject_specific = bool(subject_pool)
    background = bool(background_pool or subject_pool)

    issues: list[str] = []
    limitations: list[str] = []
    missing_tools: list[str] = []
    search_only = bool(snippets) and not fetched_pages
    profile = request_profile(question)
    corpus = "\n".join(
        (getattr(item, "content", "") or "") + " " + (getattr(item, "source_ref", "") or "")
        for item in citable
    )
    missing_named = [
        term for term in named_entities(question)
        if term.lower() not in corpus.lower()
    ]
    matched_all = {
        match for rel in relevance_by_id.values() for match in rel.matched_terms
    }
    missing_terms = (
        [
            term for term in sorted(subject_terms(question))
            if term not in matched_all
        ]
        if (citable or named_entities(question))
        else []
    )
    missing_subjects = tuple(dict.fromkeys((*missing_named, *missing_terms)))
    requested = requested_aspects(question)
    corpus_for_aspects = "\n".join(
        getattr(item, "content", "") or ""
        for item in (subject_pool or background_pool or fetched_pages or rag_pages)
    )
    supported_aspects, missing_aspects = _supported_aspects(question, corpus_for_aspects)

    # Competing accounts: a conflict only exists when each side is evidenced by
    # a distinct retrieved source. Otherwise the missing side is a gap, and the
    # report may not describe an unretrieved conflict. Both subject-specific and
    # background pages can state an account, so both pools are searched.
    conflict_pool: list = []
    seen_ids: set[str] = set()
    for item in [*subject_pool, *background_pool]:
        if item.id not in seen_ids:
            seen_ids.add(item.id)
            conflict_pool.append(item)
    conflicts = conflicts_for(question, conflict_pool)
    conflict_aspects: list[str] = []
    for conflict in conflicts:
        for label in conflict.missing_accounts:
            aspect = f"conflicting account: {label}"
            if aspect not in missing_aspects:
                conflict_aspects.append(aspect)
    missing_aspects = tuple(dict.fromkeys((*missing_aspects, *conflict_aspects)))

    # Requested facts the evidence may not establish at all (for example the
    # exact latest patch version).
    missing_requirements = release_requirements(question, corpus)

    qualified_possible = bool(background and (missing_subjects or missing_aspects))
    answer_as_asked = bool(
        subject_specific
        and not missing_subjects
        and not missing_aspects
        and not missing_requirements
        and not any(item.unresolved for item in conflicts)
    )
    quality = evidence_quality(len(primary), subject_specific, background)
    ceiling = confidence_ceiling(
        answer_as_asked=answer_as_asked,
        subject_specific=subject_specific,
        qualified_possible=qualified_possible,
        quality=quality,
        missing_requirements=missing_requirements,
        conflicts=conflicts,
        missing_aspects=missing_aspects,
    )
    subject_label = " ".join(named_entities(question)) or " ".join(
        sorted(subject_terms(question))
    )
    requested_subjects = tuple(
        dict.fromkeys((*named_entities(question), *sorted(subject_terms(question))))
    )
    subject_source_refs = tuple(
        dict.fromkeys(item.source_ref for item in subject_pool)
    )
    background_source_refs = tuple(
        dict.fromkeys(item.source_ref for item in background_pool)
    )

    web_enabled = bool(allowed & {"web_search", "fetch_page"})
    fetch_enabled = "fetch_page" in allowed
    rag_enabled = "rag_search" in allowed

    if not allowed:
        issues.append(
            "No research tools are enabled, so no evidence could be collected. "
            "This run cannot support a researched answer."
        )

    if route.kind == "external":
        if not web_enabled:
            missing_tools.extend(["web_search", "fetch_page"])
            issues.append(
                "Web research is disabled for this run; the question requires "
                "current external evidence, which the enabled tools cannot collect."
            )
        elif not fetched_specific:
            if background_pool:
                limitations.append(
                    "No subject-specific fetched page was recorded; only general "
                    "background is available, so the answer must be qualified."
                )
            else:
                issues.append(
                    "No relevant fetched web pages were recorded; search snippets and "
                    "irrelevant RAG chunks are not sufficient evidence."
                )
        if route.requires_official and not primary:
            issues.append(
                "The question requires official/primary sources, but no relevant "
                "successfully fetched primary-source page was recorded."
            )
        if required and supported < required:
            issues.append(
                f"The question requires {required} findings with distinct primary "
                f"sources; only {supported} qualifying source(s) were recorded."
            )
    elif route.kind == "local":
        if not rag_enabled:
            missing_tools.append("rag_search")
            issues.append(
                "Local document search is disabled for this run; the question asks "
                "about supplied documents, which cannot be inspected."
            )
        if not rag_specific:
            if rag_background:
                limitations.append(
                    "No subject-specific local passage was retrieved; only general "
                    "local background is available."
                )
            else:
                issues.append(
                    "No relevant local-document evidence was recorded for this question."
                )
        if required and supported < required:
            issues.append(
                f"The question requires {required} supported findings; only {supported} "
                "relevant local source section(s) were recorded."
            )
    else:
        # General/conceptual questions can use subject evidence or a substantial
        # background page, but one incidental keyword is never enough.
        if profile.kind in {"conceptual", "technical", "comparison", "detailed"}:
            if not subject_specific and not background_pool:
                if snippets:
                    issues.append(
                        "No fetched page content was recorded; only search-result "
                        "snippets are available for the general principle."
                    )
                else:
                    issues.append(
                        "No usable, relevant evidence was recorded for this question; "
                        "generic pages and incidental keyword matches are not evidence "
                        "about the asked subject."
                    )
            elif not subject_specific:
                limitations.append(
                    "Only general background was retrieved; no subject-specific source "
                    "supports the named system or technology."
                )
        if required and supported < required:
            issues.append(
                f"The question requires {required} supported findings; only "
                f"{supported} relevant source(s) were recorded."
            )

    if urls:
        if not fetch_enabled:
            missing_tools.append("fetch_page")
            issues.append(
                "The question names a URL, but Fetch page is disabled, so the page "
                "could not be inspected."
            )
        else:
            missing = [
                url for url in urls
                if not any(same_url(item.source_ref, url) for item in fetched_pages)
            ]
            if missing:
                issues.append(
                    "The question requires the contents of "
                    + ", ".join(missing)
                    + ", but no successful fetch of that URL was recorded."
                )
    if search_only and route.kind != "local":
        issues.append(
            "Only search-result snippets were recorded; no page content was fetched "
            "to support the requested claims."
        )
    if "fetch_page" in failed_tools and urls:
        issues.append("fetch_page failed, so the requested page was not inspected.")

    # A failed invocation is a limitation, not proof that independently
    # successful evidence does not exist. The writer may still answer from the
    # successful pages and must qualify the remaining gap.
    web_failures = _tool_failure_details(tool_report, "web_search")
    if web_failures:
        limitations.append(
            "Some web searches failed ("
            + "; ".join(web_failures)
            + "); any successfully fetched pages remain usable evidence."
        )
    fetch_failures = _tool_failure_details(tool_report, "fetch_page")
    if fetch_failures:
        limitations.append(
            "Some page fetches failed ("
            + "; ".join(fetch_failures)
            + "); only pages that returned readable content were used."
        )

    if missing_named:
        limitations.append(
            "No retrieved source mentions "
            + ", ".join(dict.fromkeys(missing_named))
            + "; claims specific to that name remain unverified."
        )
    if missing_aspects:
        limitations.append(
            "Evidence does not cover the requested part(s): "
            + ", ".join(missing_aspects)
            + "."
        )
    if missing_requirements:
        limitations.append(
            "The evidence does not establish "
            + "; ".join(missing_requirements)
            + "; the answer must state that gap instead of asserting a value."
        )
        issues.append(
            "The question requires "
            + "; ".join(missing_requirements)
            + ", which no retrieved source establishes."
        )
    for conflict in conflicts:
        if conflict.unresolved:
            limitations.append(conflict.note)
        elif conflict.genuine:
            limitations.append(conflict.note)
    if not subject_specific and background:
        missing_subjects = tuple(
            dict.fromkeys((*missing_subjects, "subject-specific source"))
        )

    return Assessment(
        ok=not issues,
        route=route.kind,
        required=required,
        supported=supported,
        distinct_sources=len({item.source_ref for item in subject_pool or background_pool}),
        primary_sources=len({item.source_ref for item in primary}),
        issues=tuple(dict.fromkeys(issues)),
        limitations=tuple(dict.fromkeys(limitations)),
        search_only=search_only,
        missing_tools=tuple(dict.fromkeys(missing_tools)),
        required_urls=urls,
        subject_specific=subject_specific,
        background=background,
        missing_subjects=missing_subjects,
        supported_aspects=supported_aspects,
        missing_aspects=missing_aspects,
        qualified_possible=qualified_possible,
        answer_as_asked=answer_as_asked,
        subject=subject_label,
        requested_subjects=requested_subjects,
        requested_subtopics=requested,
        subject_source_refs=subject_source_refs,
        background_source_refs=background_source_refs,
        missing_requirements=missing_requirements,
        conflicts=conflicts,
        evidence_quality=quality,
        ceiling=ceiling,
    )


def coverage_text(assessment: Assessment) -> str:
    """A compact, deterministic evidence-coverage statement for prompts."""
    def yes(value: bool) -> str:
        return "yes" if value else "no"

    parts = [
        "Requested subject: " + (assessment.subject or "(not named)"),
        "Subject-specific evidence: " + yes(assessment.subject_specific),
        "General background evidence: " + yes(assessment.background),
        "Evidence quality: " + assessment.evidence_quality,
        "Answer as asked: " + yes(assessment.answer_as_asked),
        "Qualified partial answer possible: " + yes(assessment.qualified_possible),
        f"Confidence ceiling: {assessment.confidence_ceiling:.2f}",
    ]
    if assessment.requested_subtopics:
        parts.append("Requested parts: " + ", ".join(assessment.requested_subtopics))
    if assessment.subject_source_refs:
        parts.append(
            "Subject-specific sources: " + ", ".join(assessment.subject_source_refs[:5])
        )
    if assessment.background_source_refs:
        parts.append(
            "Background-only sources: " + ", ".join(assessment.background_source_refs[:5])
        )
    if assessment.supported_aspects:
        parts.append("Covered parts: " + ", ".join(assessment.supported_aspects))
    if assessment.missing_aspects:
        parts.append("Missing parts: " + ", ".join(assessment.missing_aspects))
    if assessment.missing_subjects:
        parts.append("Missing subjects: " + ", ".join(assessment.missing_subjects))
    if assessment.missing_requirements:
        parts.append("Unestablished requirements: " + "; ".join(assessment.missing_requirements))
    for conflict in assessment.conflicts:
        parts.append("Conflicting claims: " + conflict.note)
    return "; ".join(parts)


def partial_snippet_evidence(question: str, items, assessment: Assessment) -> bool:
    """True when relevant snippets can support a clearly qualified partial answer.

    This never upgrades a snippet to fetched-page evidence. It only permits a
    general, explicitly qualified answer when no page body was available.
    """
    if not assessment.search_only:
        return False
    return any(
        getattr(item, "source_type", "") == "web"
        and str(getattr(item, "id", "")).startswith("web-")
        and len((getattr(item, "content", "") or "").strip()) >= MIN_FETCHED_CHARS
        and relevance(getattr(item, "content", ""), question).relevant
        for item in (items or [])
    )


def gap_answer(question: str, assessment: Assessment) -> str:
    requirement = (
        f"{assessment.required} requested finding(s)" if assessment.required
        else "the requested evidence"
    )
    detail = "; ".join(assessment.issues) or "the available evidence is incomplete."
    return (
        f"The available evidence does not satisfy {requirement}. {detail} "
        "I have not inferred findings from titles, snippets, or unrelated documents, "
        "and I have not invented sources or URLs. A further research pass is needed "
        "to resolve the unsupported parts of the question."
    )
