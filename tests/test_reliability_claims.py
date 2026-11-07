"""Deterministic regression tests for claim-level validation (item B).

Each test drives :mod:`athena.agents.claim_support` with a fixed evidence body and
a fixed report sentence: no model, no network, no clock.
"""

from __future__ import annotations

from athena.agents.claim_support import (
    advisory_problems,
    capability_escalation,
    claim_problems,
    exceeds_evidence,
    is_qualification,
    qualifier_dropped,
    remove_unsupported,
    superlatives,
    support_score,
    unsupported_specifics,
    validate_claims,
)
from athena.blackboard import Citation, EvidenceItem

QUESTION = "What is the latest stable Python release version?"
EVIDENCE_TEXT = (
    "Python 3.14 is the current stable release of Python. The Python 3.14 release "
    "shipped with an improved interpreter and faster startup, and the release notes "
    "describe the changed behavior. About 15 percent faster than Python 3.13 in the "
    "benchmark the maintainers published. "
)
ITEMS = [
    EvidenceItem(
        id="fetch-1",
        source_type="web",
        source_ref="https://docs.python.org/3/",
        content=EVIDENCE_TEXT,
        retrieved_by="web_researcher",
    )
]
CITATION = Citation(
    source_type="web",
    source_ref="https://docs.python.org/3/",
    quote_or_summary="Python 3.14 is the current stable release",
)


def _check(sentence: str, citations=(CITATION,), question: str = QUESTION):
    checks = validate_claims([sentence], ITEMS, question, list(citations))
    assert len(checks) == 1
    return checks[0]


# --- the source supports the claim -------------------------------------------


def test_a_supported_claim_passes_every_check() -> None:
    check = _check("Python 3.14 is the current stable release of Python.")

    assert check.supported is True
    assert check.citation_mismatch is False
    assert check.exceeds == ()
    assert check.dropped_qualifier is False
    assert check.entity_relevant is True
    assert check.ok is True
    assert check.supporting_refs == ("https://docs.python.org/3/",)


def test_a_claim_no_recorded_source_supports_fails() -> None:
    check = _check("Hall-effect thrusters accelerate xenon ions with an electric field.")

    assert check.supported is False
    assert check.ok is False
    assert "no recorded source supports it" in check.describe()


# --- entity relevance --------------------------------------------------------


def test_a_sentence_about_a_different_subject_is_not_entity_relevant() -> None:
    """A true statement is still not an answer about the asked subject."""
    check = _check(
        "The first Hall-effect thruster flew on a Soviet satellite in 1971."
    )

    assert check.entity_relevant is False
    assert check.ok is False


# --- the citation must point at a source that supports the sentence ----------


def test_citation_pointing_at_the_wrong_source_is_a_mismatch() -> None:
    wrong = Citation(
        source_type="web",
        source_ref="https://example.com/other",
        quote_or_summary="unrelated",
    )
    items = ITEMS + [
        EvidenceItem(
            id="fetch-2",
            source_type="web",
            source_ref="https://example.com/other",
            content="An unrelated page about weather and travel advice for the week. " * 3,
            retrieved_by="web_researcher",
        )
    ]

    check = validate_claims(
        ["Python 3.14 is the current stable release of Python."],
        items,
        QUESTION,
        [wrong],
    )[0]

    assert check.citation_mismatch is True
    assert "no cited source supports it" in check.describe()


def test_a_citation_that_does_support_the_claim_is_not_a_mismatch() -> None:
    assert _check("Python 3.14 is the current stable release of Python.").citation_mismatch is False


# --- unsupported patch numbers, dates and specifications ---------------------


def test_unsupported_patch_number_and_date_are_flagged() -> None:
    claim = "Python 3.14.7 is the current stable release and shipped in 2027."

    missing = unsupported_specifics(claim, EVIDENCE_TEXT)

    assert "3.14.7" in missing
    assert "2027" in missing
    assert exceeds_evidence(claim, EVIDENCE_TEXT)  # "states 3.14.7, 2027 which..."
    check = _check(claim)
    assert check.ok is False
    assert check.supported is False  # the invented version is not in any source


def test_supported_numbers_are_not_flagged() -> None:
    hedged = "About 15 percent faster than Python 3.13 in the benchmark."

    assert unsupported_specifics(hedged, EVIDENCE_TEXT) == ()
    assert _check(hedged).exceeds == ()
    # The same figure without the source's "about" is a precision the source did
    # not state, and that is caught separately.
    assert qualifier_dropped(
        "15 percent faster than Python 3.13 in the benchmark.", EVIDENCE_TEXT
    ) is True


def test_an_unsupported_specification_is_flagged() -> None:
    text = (
        "The thruster is a monopropellant hydrazine unit used for attitude control "
        "and it has been flown on several missions. " * 3
    )
    items = [
        EvidenceItem(
            id="fetch-1",
            source_type="web",
            source_ref="https://example.com/thruster",
            content=text,
            retrieved_by="web_researcher",
        )
    ]
    claim = "The thruster produces 400 N of thrust in flight."

    check = validate_claims([claim], items, "What thrust does the thruster produce?", [])[0]

    assert "400" in unsupported_specifics(claim, text)
    assert check.supported is False
    assert check.ok is False


# --- qualifiers are kept -----------------------------------------------------


def test_dropping_the_sources_qualifier_is_flagged() -> None:
    dropped = "The interpreter is 15 percent faster than Python 3.13."
    kept = "The interpreter is about 15 percent faster than Python 3.13."

    assert qualifier_dropped(dropped, EVIDENCE_TEXT) is True
    assert qualifier_dropped(kept, EVIDENCE_TEXT) is False
    assert _check(dropped).dropped_qualifier is True
    assert _check(dropped).ok is False
    assert _check(kept).ok is True


def test_an_explicit_qualification_sentence_is_accepted() -> None:
    sentence = "No retrieved source states the exact patch version."

    assert is_qualification(sentence) is True
    check = _check(sentence)
    assert check.supported is True
    assert check.ok is True


# --- absolute and escalated wording -----------------------------------------


def test_a_superlative_the_evidence_never_uses_is_reported() -> None:
    claim = "Python 3.14 is the fastest Python release ever shipped."

    found = superlatives(claim, EVIDENCE_TEXT)
    assert found
    assert advisory_problems(claim, [EVIDENCE_TEXT])
    assert exceeds_evidence(claim, EVIDENCE_TEXT)


def test_a_planned_capability_asserted_as_existing_is_reported() -> None:
    evidence = (
        "The Draco thruster is planned for the next crewed capsule, and the design "
        "is still a prototype under development at the vendor's facility. " * 3
    )

    assert capability_escalation("The Draco thruster is used on every flight.", evidence) is True
    assert capability_escalation("The Draco thruster is planned for the capsule.", evidence) is False


# --- removal keeps the answer honest ----------------------------------------


def test_unsupported_sentences_are_removed_and_qualified_ones_kept() -> None:
    good = "Python 3.14 is the current stable release of Python."
    bad = "Python 3.14.7 shipped in 2027 with a rewritten compiler."

    kept, removed = remove_unsupported(f"{good} {bad}", [EVIDENCE_TEXT])

    assert good in kept
    assert bad in removed
    assert "3.14.7" not in kept


def test_claim_problems_are_deterministic_and_descriptive() -> None:
    checks = validate_claims(
        [
            "Python 3.14 is the current stable release of Python.",
            "Python 3.14.7 is the current stable release and shipped in 2027.",
        ],
        ITEMS,
        QUESTION,
        [CITATION],
    )

    problems = claim_problems(checks)

    assert len(problems) == 1
    assert "3.14.7" in problems[0]
    assert claim_problems(checks) == problems  # stable ordering, no duplicates


def test_support_score_is_bounded_and_numbers_must_match() -> None:
    assert 0.0 <= support_score("Python 3.14 is the current stable release", EVIDENCE_TEXT) <= 1.0
    assert support_score("Python 3.15.1 is the current stable release", EVIDENCE_TEXT) == 0.0
    assert support_score("short claim", EVIDENCE_TEXT) == 1.0  # not a factual claim
