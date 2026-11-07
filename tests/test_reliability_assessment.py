"""Deterministic regression tests for evidence assessment (items A and E).

Every test is offline: evidence items are constructed in the test, no tool, model
or network call is made. What is asserted is the *assessment*, because that is
what gates the writer, the critic and the confidence ceiling.
"""

from __future__ import annotations

from athena.agents.sufficiency import (
    Assessment,
    conflicts_for,
    coverage_text,
    evidence_quality,
    release_requirements,
    requested_aspects,
)
from athena.agents.sufficiency import assess
from athena.blackboard import EvidenceItem

HALL_QUESTION = (
    "Explain how a Hall-effect thruster works and what propellant it uses."
)
DRACO_CONFLICT_QUESTION = (
    "What propellant does SpaceX's Draco thruster use? Some sources describe it as "
    "a nitrogen cold-gas thruster, while others describe a hypergolic engine."
)
WEB = ["web_search", "fetch_page"]


def _fetch(index: int, ref: str, content: str) -> EvidenceItem:
    return EvidenceItem(
        id=f"fetch-{index}",
        source_type="web",
        source_ref=ref,
        content=content,
        retrieved_by="web_researcher",
    )


def _rag(index: int, ref: str, content: str) -> EvidenceItem:
    return EvidenceItem(
        id=f"doc_{index}",
        source_type="rag",
        source_ref=ref,
        content=content,
        retrieved_by="rag_researcher",
    )


GENERIC_ROCKET_PAGE = (
    "Rocket engines are used to launch payloads into orbit. Propellant choices "
    "include solids, liquids and gases, and engineers choose among them for cost, "
    "handling and performance reasons that differ from mission to mission. " * 3
)
COLD_GAS_BACKGROUND = (
    "A nitrogen cold gas thruster stores nitrogen gas under pressure and releases "
    "it through valves and nozzles to produce low thrust for small spacecraft. " * 3
)
BACKGROUND_PRIMER = (
    "Nitrogen thrusters work by expelling gas through a nozzle. Engineers pick one "
    "for a mission because the gas is cheap and the hardware is simple to operate, "
    "although the thrust available stays small. " * 3
)
COLD_GAS_SPECIFIC = (
    "The SpaceX Draco thruster is a nitrogen cold gas thruster: pressurised "
    "nitrogen is released through valves and a nozzle to produce thrust. " * 3
)
HYPERGOLIC_SPECIFIC = (
    "The SpaceX Draco thruster is hypergolic: it burns monomethylhydrazine (MMH) "
    "with nitrogen tetroxide (N2O4) and ignites on contact, with no igniter. " * 3
)


# --- A: what was requested, and what the evidence actually covers --------------


def test_assessment_names_the_requested_subject_and_subtopics() -> None:
    assessment = assess(
        HALL_QUESTION, [_fetch(1, "https://example.com/hall", GENERIC_ROCKET_PAGE)],
        enabled_tools=WEB,
    )

    assert "hall" in assessment.requested_subjects
    assert "thruster" in assessment.requested_subjects
    assert {"mechanism", "propellant"} <= set(assessment.requested_subtopics)
    assert assessment.subject  # a human-readable subject label is recorded


def test_one_matching_word_on_a_generic_page_does_not_prove_a_named_claim() -> None:
    """The Hall-effect thruster case: a page about thrusters in general."""
    page = (
        "A thruster is a propulsion device. Many spacecraft carry thrusters of "
        "different kinds, and engineers compare their cost and complexity at "
        "length before choosing one for a given mission profile. " * 3
    )

    assessment = assess(
        HALL_QUESTION, [_fetch(1, "https://example.com/thrusters", page)],
        enabled_tools=WEB,
    )

    assert assessment.subject_specific is False
    assert assessment.answer_as_asked is False
    assert assessment.ok is False
    assert assessment.confidence_ceiling <= 0.45
    assert "hall" in assessment.missing_subjects


def test_generic_source_with_one_matching_word_is_never_a_named_entity_claim() -> None:
    """SpaceX Draco: a generic liquid-engine primer is not evidence about Draco."""
    primer = (
        "Liquid rocket engines use fuel and oxidiser in separate tanks. They may be "
        "pressure-fed or pump-fed and can be throttled in flight. Liquid propulsion "
        "can reach a higher specific impulse than solid propulsion does. " * 3
    )
    question = (
        "How does SpaceX's Draco propulsion system work? Explain its propellants, "
        "operating principle, and role in spacecraft control."
    )

    assessment = assess(
        question, [_fetch(1, "https://example.com/liquids", primer)],
        enabled_tools=WEB,
    )

    assert assessment.subject_specific is False
    assert assessment.answer_as_asked is False
    assert assessment.missing_subjects  # the named system and terms are reported
    assert any("Draco" in item or "SpaceX" in item for item in assessment.missing_subjects)


def test_subject_specific_and_background_sources_are_separated() -> None:
    question = "How does SpaceX's nitrogen thruster work in space?"
    specific = _fetch(
        1,
        "https://example.com/spacex-thruster",
        "SpaceX's nitrogen thruster expands nitrogen gas through a nozzle to make "
        "thrust without combustion. " * 3,
    )
    background = _fetch(2, "https://example.com/thruster-primer", BACKGROUND_PRIMER)

    assessment = assess(question, [specific, background], enabled_tools=WEB)

    assert assessment.subject_specific is True
    assert assessment.background is True
    assert assessment.subject_source_refs == ("https://example.com/spacex-thruster",)
    assert assessment.background_source_refs == ("https://example.com/thruster-primer",)


def test_background_only_evidence_is_qualified_not_the_answer_as_asked() -> None:
    question = "How does SpaceX's nitrogen thruster work in space?"
    background = _fetch(1, "https://example.com/thruster-primer", BACKGROUND_PRIMER)

    assessment = assess(question, [background], enabled_tools=WEB)

    assert assessment.subject_specific is False
    assert assessment.background is True
    assert assessment.answer_as_asked is False
    # A partial, explicitly qualified answer is possible, but never at high
    # confidence and never without naming the missing subject evidence.
    assert assessment.confidence_ceiling <= 0.5
    assert "subject-specific source" in assessment.missing_subjects


def test_missing_requested_aspect_is_listed_and_caps_the_ceiling() -> None:
    """A page can be about the subject and still not cover a requested part."""
    question = "How does a nitrogen cold gas thruster work and what propellant does it use?"
    no_mechanism = _fetch(
        1,
        "https://example.com/cold-gas-cost",
        "A nitrogen cold gas thruster is cheap to build and to qualify for a "
        "mission. Programs choose it because it is simple and reliable when the "
        "required thrust is small. " * 3,
    )

    assessment = assess(question, [no_mechanism], enabled_tools=WEB)

    assert "mechanism" in assessment.requested_subtopics
    assert "mechanism" in assessment.missing_aspects
    assert assessment.answer_as_asked is False
    assert assessment.confidence_ceiling <= 0.6
    assert any("does not cover" in item for item in assessment.limitations)


def test_evidence_quality_is_named_not_guessed() -> None:
    assert evidence_quality(2, True, True) == "primary"
    assert evidence_quality(0, True, True) == "secondary"
    assert evidence_quality(0, False, True) == "background"
    assert evidence_quality(0, False, False) == "none"


def test_coverage_text_states_subject_specific_and_background_coverage() -> None:
    assessment = assess(
        "How does a nitrogen cold gas thruster work?",
        [_fetch(1, "https://example.com/cold-gas", COLD_GAS_BACKGROUND)],
        enabled_tools=WEB,
    )

    text = coverage_text(assessment)

    assert "Requested subject:" in text
    assert "Subject-specific evidence: yes" in text
    assert "Evidence quality: primary" in text
    assert "Confidence ceiling:" in text


# --- E: requested conflicts, and saying so when no pair was retrieved ---------


def test_conflicting_accounts_need_both_sides_from_distinct_sources() -> None:
    cold_only = conflicts_for(DRACO_CONFLICT_QUESTION, [_cold_specific()])

    assert len(cold_only) == 1
    conflict = cold_only[0]
    assert conflict.genuine is False
    assert conflict.unresolved is True
    assert set(conflict.missing_accounts)  # the unevidenced side is named
    assert "no genuinely conflicting credible source pair was retrieved" in conflict.note

    both = conflicts_for(
        DRACO_CONFLICT_QUESTION, [_cold_specific(), _hypergolic_specific()]
    )[0]

    assert both.genuine is True
    assert both.unresolved is False
    assert both.missing_accounts == ()
    assert len(set(both.supporting_refs)) == 2
    assert "attribute each account to its source" in both.note


def _cold_specific() -> EvidenceItem:
    return _fetch(1, "https://example.com/draco-cold-gas", COLD_GAS_SPECIFIC)


def _hypergolic_specific() -> EvidenceItem:
    return _fetch(2, "https://example.com/draco-hypergolic", HYPERGOLIC_SPECIFIC)


def test_conflict_detection_uses_background_sources_too() -> None:
    """A background page can state one account; the pool must not drop it.

    Regression: if only subject-specific pages are searched for the competing
    accounts, this question can never be answered as a genuine conflict.
    """
    question = DRACO_CONFLICT_QUESTION
    background_cold = _fetch(1, "https://example.com/cold-gas", COLD_GAS_BACKGROUND)
    specific_hot = _fetch(2, "https://example.com/draco", HYPERGOLIC_SPECIFIC)

    assessment = assess(question, [background_cold, specific_hot], enabled_tools=WEB)

    # The cold-gas page is background-only: it never names SpaceX or Draco.
    assert "https://example.com/cold-gas" in assessment.background_source_refs
    assert "https://example.com/cold-gas" not in assessment.subject_source_refs

    conflict = assessment.conflicts[0]
    assert conflict.genuine is True
    assert set(conflict.supporting_refs) == {
        "https://example.com/cold-gas",
        "https://example.com/draco",
    }


def test_no_retrieved_source_supports_either_account_says_so() -> None:
    unrelated = _fetch(
        1,
        "https://example.com/weather",
        "Tomorrow will be sunny with a light breeze and a high of twenty degrees. " * 3,
    )

    conflict = conflicts_for(DRACO_CONFLICT_QUESTION, [unrelated])[0]

    assert conflict.supported_accounts == ()
    assert conflict.genuine is False
    assert "No retrieved source supports either competing account" in conflict.note

    assessment = assess(DRACO_CONFLICT_QUESTION, [unrelated], enabled_tools=WEB)
    assert assessment.unresolved_conflicts
    assert any(
        "competing account" in note for note in assessment.limitations
    ), assessment.limitations


def test_an_unresolved_conflict_caps_confidence_and_is_reported() -> None:
    question = DRACO_CONFLICT_QUESTION
    one_sided = _fetch(1, "https://example.com/draco-cold-gas", COLD_GAS_SPECIFIC)

    assessment = assess(question, [one_sided], enabled_tools=WEB)

    assert assessment.unresolved_conflicts
    assert assessment.confidence_ceiling <= 0.45
    missing = [item for item in assessment.missing_aspects if item.startswith("conflicting account:")]
    assert missing, assessment.missing_aspects
    text = coverage_text(assessment)
    assert "Conflicting claims:" in text


def test_a_genuine_conflict_is_reported_without_capping_as_unresolved() -> None:
    question = DRACO_CONFLICT_QUESTION
    items = [_fetch(1, "https://example.com/draco-cold-gas", COLD_GAS_SPECIFIC),
             _fetch(2, "https://example.com/draco-hypergolic", HYPERGOLIC_SPECIFIC)]

    assessment = assess(question, items, enabled_tools=WEB)

    assert assessment.conflicts[0].genuine is True
    assert assessment.unresolved_conflicts == ()
    assert any("attribute each account" in note for note in assessment.limitations)


# --- request parsing used by the other checks --------------------------------


def test_requested_aspects_and_release_requirement_are_deterministic() -> None:
    assert "mechanism" in requested_aspects(HALL_QUESTION)
    assert "propellant" in requested_aspects(HALL_QUESTION)

    shallow = release_requirements(
        "What is the latest stable Python release version?",
        "Python 3.14 is the latest stable release.",
    )
    assert shallow and "patch version" in shallow[0]

    deep = release_requirements(
        "What is the latest stable Python release version?",
        "Python 3.14.2 is the latest stable release.",
    )
    assert deep == ()


def test_assessment_is_a_frozen_record_of_the_decision() -> None:
    assessment = assess(
        "How does a nitrogen cold gas thruster work?",
        [_rag(1, "handbook.pdf#p4", COLD_GAS_BACKGROUND)],
        enabled_tools=["rag_search"],
    )

    assert isinstance(assessment, Assessment)
    assert assessment.route == "general"
    assert isinstance(assessment.confidence_ceiling, float)
