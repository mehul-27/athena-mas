"""Source routing and evidence-sufficiency gate tests (offline)."""

from __future__ import annotations

from athena.agents.crew_writer import write_report, writer_prompt
from athena.agents import researcher as researcher_module
from athena.agents.planner import prepare_plan
from athena.agents.routing import (
    classify,
    ensure_step_texts,
    is_primary_source,
    is_relevant,
    preferred_roles,
    request_profile,
    source_guidance,
)
from athena.agents.sufficiency import assess
from athena.agents.critique_rules import calibrate
from athena.blackboard import Blackboard, Critique, EvidenceItem, FinalReport, SQLiteStore, Task
from athena.observability.events import Event
from athena.observability.store import SQLiteEventStore

PYTHON_QUESTION = (
    "Explain three key changes introduced in Python 3.13, with one official "
    "source URL for each and a brief practical impact."
)


def _evidence(evidence_id: str, source_type: str, source_ref: str, content: str):
    return EvidenceItem(
        id=evidence_id,
        source_type=source_type,
        source_ref=source_ref,
        content=content,
        retrieved_by="researcher",
    )


def _official(index: int):
    return _evidence(
        f"fetch-{index}",
        "web",
        f"https://docs.python.org/3.13/whatsnew/{index}.html",
        f"Python 3.13 change {index}: " + "supported explanation and impact " * 20,
    )


def test_question_routing_separates_local_and_external_sources() -> None:
    assert classify(PYTHON_QUESTION).kind == "external"
    assert classify(PYTHON_QUESTION).needs_web is True

    local = "According to the supplied handbook, what does configuration management cover?"
    assert classify(local).kind == "local"
    assert classify(local).needs_rag is True

    assert classify("What is systems engineering?").kind == "general"


def test_researcher_prompt_prefers_web_for_technical_questions() -> None:
    assert "Start with" in source_guidance(PYTHON_QUESTION)
    assert "RAG call for every question" in researcher_module.INSTRUCTION
    assert "Only call `web_search`" not in researcher_module.INSTRUCTION


def test_thruster_question_is_not_misclassified_by_the_word_rust() -> None:
    question = (
        "how does spacex nitrogen thruster work in space? "
        "whats the working behind them"
    )

    assert classify(question).kind == "general"
    profile = request_profile(question)
    assert profile.kind == "conceptual"
    assert profile.allows_calculation is False
    assert profile.allows_forecast is False
    assert profile.notes and "Roadster" in profile.notes[0]
    assert "conceptual/technical" in source_guidance(question)
    assert preferred_roles(question, ("rag_researcher", "web_researcher")) == [
        "web_researcher"
    ]
    assert any(
        step.startswith("web_researcher:")
        for step in ensure_step_texts([], question)
    )


def test_writer_prompt_requires_vehicle_qualification_for_ambiguous_spacex_question(
    tmp_path,
) -> None:
    board = Blackboard.create(
        Task(
            question=(
                "how does spacex nitrogen thruster work in space? "
                "whats the working behind them"
            )
        ),
        store=SQLiteStore(tmp_path / "bb.db"),
    )

    prompt = writer_prompt(board, "write")

    assert "Roadster" in prompt
    assert "demonstrator" in prompt
    assert "flight hardware" in prompt


def test_conceptual_plan_drops_unrequested_calculation() -> None:
    policy = prepare_plan(
        [
            "web_researcher: search official documentation and fetch pages",
            "calc_researcher: compute working numbers",
        ],
        "how does a nitrogen cold gas thruster work in space?",
    )

    assert [step.assigned_to for step in policy.steps] == ["web_researcher"]
    assert any("Calculation/forecast steps were omitted" in item
               for item in policy.limitations)


def test_quantitative_plan_keeps_a_calculation_step() -> None:
    policy = prepare_plan(
        ["calc_researcher: calculate force from the stated mass flow and velocity"],
        "Calculate thrust from a 10 kg/s mass flow at 300 m/s.",
    )

    assert [step.assigned_to for step in policy.steps] == ["calc_researcher"]
    assert request_profile("What is 2 + 2?").allows_calculation is True


def test_python_3_13_failure_regression_requires_official_fetches() -> None:
    """The observed failure: irrelevant RAG only, no fetched official pages."""
    plan = ensure_step_texts(
        ["rag_researcher: search the NASA handbook"], PYTHON_QUESTION
    )
    assert any(step.startswith("web_researcher:") for step in plan)

    bad = assess(PYTHON_QUESTION, [
        _evidence("doc_1", "rag", "nasa_Systems_handbook.pdf#p1", "configuration management"),
    ])
    good = assess(PYTHON_QUESTION, [_official(1), _official(2), _official(3)])

    assert bad.ok is False
    assert good.ok is True
    assert good.supported == 3
    assert good.primary_sources == 3



def test_plan_gets_a_required_source_step_without_duplicates() -> None:
    external = ensure_step_texts(["rag_researcher: search corpus"], PYTHON_QUESTION)
    assert any(step.startswith("web_researcher:") for step in external)
    assert sum(step.startswith("web_researcher:") for step in external) == 1

    local = ensure_step_texts(
        ["web_researcher: search"], "According to the supplied handbook, what is CM?"
    )
    assert any(step.startswith("rag_researcher:") for step in local)


def test_relevance_and_primary_source_heuristics() -> None:
    assert is_relevant("Python 3.13 release notes", PYTHON_QUESTION)
    assert not is_relevant("NASA configuration management", PYTHON_QUESTION)
    assert is_primary_source("https://docs.python.org/3.13/")
    assert is_primary_source("https://peps.python.org/pep-0703/")
    assert not is_primary_source("https://www.geeksforgeeks.org/python-3-13/")
    assert not is_primary_source("ftp://docs.python.org/3.13/")


def test_irrelevant_rag_alone_is_insufficient_for_external_question() -> None:
    assessment = assess(PYTHON_QUESTION, [
        _evidence("doc_1", "rag", "nasa_Systems_handbook.pdf#p1-chunk-2", "configuration management"),
        _evidence("other-1", "other", "http://127.0.0.1/blocked", "fetch status: blocked"),
    ])

    assert assessment.ok is False
    assert any("No relevant fetched web pages" in issue for issue in assessment.issues)


def test_critic_calibration_uses_the_same_coverage_ceiling() -> None:
    subject = assess(PYTHON_QUESTION, [_official(1), _official(2), _official(3)])
    missing = assess(
        "What propellant does SpaceX's Draco thruster use?",
        [
            _fetch(
                1,
                "https://example.com/history",
                "A historical Draco was a 1959 test vehicle. " * 4,
            )
        ],
    )
    background = assess(
        "How does SpaceX's nitrogen thruster work in space?",
        [
            _fetch(
                1,
                "https://example.com/cold-gas-guide",
                "A cold gas thruster expands pressurized nitrogen through a nozzle "
                "to produce thrust without combustion. " * 3,
            )
        ],
    )
    approving = Critique(issues=[], severity="low", approved=True)
    qualified = FinalReport(
        answer="The evidence does not establish the named vehicle; this remains unverified.",
        key_points=["General background only."],
        citations=[],
        confidence=0.5,
        run_id="r",
    )
    overclaim = qualified.model_copy(update={"confidence": 0.8})

    assert calibrate(approving, overclaim, missing).approved is False
    assert calibrate(approving, qualified, background).approved is True
    assert calibrate(approving, overclaim, background).approved is False
    assert calibrate(approving, overclaim, subject).approved is True


def test_confidence_ceiling_tracks_subject_specific_coverage() -> None:
    subject = assess(PYTHON_QUESTION, [_official(1), _official(2), _official(3)])
    background = assess(
        "How does SpaceX's nitrogen thruster work in space?",
        [
            _fetch(
                1,
                "https://example.com/cold-gas-guide",
                "A cold gas thruster expands pressurized nitrogen through a nozzle "
                "to produce thrust without combustion. " * 3,
            )
        ],
    )
    empty = assess(PYTHON_QUESTION, [])

    assert subject.subject_specific is True
    assert subject.confidence_ceiling > 0.5
    assert background.answer_as_asked is False
    assert background.confidence_ceiling <= 0.5
    assert empty.confidence_ceiling == 0.3


def test_three_distinct_official_fetched_pages_satisfy_the_gate() -> None:
    assessment = assess(PYTHON_QUESTION, [_official(1), _official(2), _official(3)])

    assert assessment.ok is True
    assert assessment.supported == 3
    assert assessment.primary_sources == 3
    assert assessment.required == 3


def test_duplicate_or_secondary_sources_do_not_meet_the_count() -> None:
    duplicate = _official(1).model_copy(update={"id": "fetch-duplicate"})
    secondary = _evidence(
        "fetch-secondary", "web", "https://www.geeksforgeeks.org/python-3-13-new-features/",
        "Python 3.13 change: " + "snippet and discussion " * 20,
    )

    assessment = assess(PYTHON_QUESTION, [_official(1), duplicate, secondary])

    assert assessment.ok is False
    assert assessment.supported < 3
    assert any("requires 3 findings" in issue for issue in assessment.issues)


def test_empty_or_failed_fetches_do_not_satisfy_the_requirement() -> None:
    empty = _evidence(
        "fetch-empty", "web", "https://docs.python.org/3.13/empty",
        "",
    )
    failed = _evidence(
        "fetch-failed", "other", "https://docs.python.org/3.13/failed",
        "fetch status: blocked",
    )

    assessment = assess(PYTHON_QUESTION, [empty, failed])

    assert assessment.ok is False
    assert assessment.supported == 0
    assert any("No relevant fetched web pages" in issue for issue in assessment.issues)



def _fetch(index: int, ref: str, content: str):
    return _evidence(
        f"fetch-mixed-{index}",
        "web",
        ref,
        content,
    )


def test_search_failure_does_not_invalidate_a_successful_fetch() -> None:
    question = "What changed in Python 3.13?"
    evidence = [
        _fetch(
            1,
            "https://docs.python.org/3.13/whatsnew/3.13.html",
            "Python 3.13 release notes explain the changed behavior. " * 4,
        )
    ]
    report = {
        "failed": ["web_search"],
        "calls": [{"tool": "web_search", "ok": False, "error": "provider throttled"}],
    }

    assessment = assess(question, evidence, enabled_tools=None, tool_report=report)

    assert assessment.ok is True
    assert assessment.issues == ()
    assert any("web_search" in item for item in assessment.limitations)


def test_generic_nasa_overview_is_not_hall_effect_evidence() -> None:
    question = "How does a Hall-effect thruster work, and why is it efficient?"
    overview = _fetch(
        1,
        "https://www.nasa.gov/glenn/glenn-expertise-space-exploration/",
        "NASA explores space and develops electric propulsion for missions. "
        "Its most capable in-space system produces about one pound of thrust. "
        "The agency studies many spacecraft technologies. " * 2,
    )

    assessment = assess(question, [overview], enabled_tools=["web_search", "fetch_page"])

    assert assessment.subject_specific is False
    assert assessment.answer_as_asked is False
    assert assessment.ok is False


def test_generic_liquid_engine_primer_is_not_spacex_draco_evidence() -> None:
    question = (
        "How does SpaceX's Draco propulsion system work? Explain its propellants, "
        "operating principle, and role in spacecraft control."
    )
    primer = _fetch(
        1,
        "https://engineering.purdue.edu/~propulsi/propulsion/rockets/liquids.html",
        "Liquid rocket engines use fuel and oxidizer in separate tanks. "
        "They may be pressure-fed or pump-fed and can be throttled. "
        "Liquid propulsion can provide higher specific impulse than solids. " * 2,
    )

    assessment = assess(question, [primer], enabled_tools=["web_search", "fetch_page"])

    assert assessment.subject_specific is False
    assert assessment.answer_as_asked is False
    assert assessment.ok is False


def test_historical_draco_name_collision_is_not_spacex_evidence() -> None:
    question = (
        "What propellant does SpaceX's Draco thruster use? Some sources describe "
        "it as a nitrogen cold-gas thruster, while others describe a hypergolic engine."
    )
    collision = _fetch(
        1,
        "https://www.astronautix.com/d/draco.html",
        "Draco was a 1959 American two-stage test vehicle. "
        "It used a TX-20 Sergeant and a TX-30 stage. "
        "The entry is a historical rocket catalogue record. " * 2,
    )

    assessment = assess(question, [collision], enabled_tools=["web_search", "fetch_page"])

    assert assessment.subject_specific is False
    assert assessment.answer_as_asked is False
    assert assessment.ok is False
    assert "SpaceX" in " ".join(assessment.missing_subjects)


def test_sentence_initial_words_are_not_named_entities() -> None:
    from athena.agents.routing import named_entities

    assert named_entities(
        "Some sources describe SpaceX's Draco thruster. Reconcile those accounts."
    ) == ["SpaceX", "Draco"]


def test_missing_organization_specific_source_is_a_nonblocking_limitation() -> None:
    question = (
        "How does SpaceX's nitrogen thruster work in space? "
        "What's the working principle behind it?"
    )
    evidence = [
        _fetch(
            1,
            "https://example.com/cold-gas-guide",
            "A cold gas thruster expands pressurized nitrogen through a nozzle "
            "to produce thrust without combustion. " * 3,
        )
    ]

    assessment = assess(question, evidence, enabled_tools=["web_search", "fetch_page"])

    assert assessment.ok is True
    assert assessment.answer_as_asked is False
    assert assessment.qualified_possible is True
    assert assessment.confidence_ceiling <= 0.5
    assert any("SpaceX" in item for item in assessment.limitations)


def test_search_success_with_all_fetches_failed_is_insufficient() -> None:
    evidence = [
        _evidence("web-1", "web", "https://docs.python.org/3.13/", "Python 3.13 release snippet"),
        _evidence("fetch-1", "other", "https://docs.python.org/3.13/", "fetch status: http_error\nHTTP 403"),
    ]
    report = {
        "failed": ["fetch_page"],
        "calls": [{"tool": "fetch_page", "ok": False, "error": "HTTP 403"}],
    }

    assessment = assess("What changed in Python 3.13?", evidence, tool_report=report)

    assert assessment.ok is False
    assert any("No relevant fetched web pages" in issue for issue in assessment.issues)
    assert any("Only search-result snippets" in issue for issue in assessment.issues)
    assert any("fetch_page" in item for item in assessment.limitations)


def test_search_snippets_alone_are_insufficient() -> None:
    evidence = [
        _evidence("web-1", "web", "https://docs.python.org/3.13/", "Python 3.13 snippet")
    ]

    assessment = assess("What changed in Python 3.13?", evidence)

    assert assessment.ok is False
    assert assessment.search_only is True
    assert any("Only search-result snippets" in issue for issue in assessment.issues)


def test_empty_and_irrelevant_fetches_are_not_usable() -> None:
    question = "What changed in Python 3.13?"
    empty = _evidence("fetch-empty", "web", "https://docs.python.org/3.13/empty", "")
    irrelevant = _fetch(
        2,
        "https://example.com/weather",
        "Tomorrow will be sunny with a light breeze. " * 5,
    )

    assessment = assess(question, [empty, irrelevant], enabled_tools=None)

    assert assessment.ok is False
    assert any("No relevant fetched web pages" in issue for issue in assessment.issues)


def test_mixed_successful_and_failed_fetch_is_sufficient_with_a_limitation() -> None:
    question = "What changed in Python 3.13?"
    good = _fetch(
        1,
        "https://docs.python.org/3.13/whatsnew/3.13.html",
        "Python 3.13 release notes explain the changed behavior. " * 4,
    )
    failed = _evidence(
        "fetch-failed", "other", "https://docs.python.org/3.13/missing",
        "fetch status: http_error\nHTTP 404",
    )
    report = {
        "failed": ["fetch_page"],
        "calls": [{"tool": "fetch_page", "ok": False, "error": "HTTP 404"}],
    }

    assessment = assess(question, [good, failed], tool_report=report)

    assert assessment.ok is True
    assert any("fetch_page" in item for item in assessment.limitations)


def test_explicit_url_not_fetched_is_a_blocking_gap() -> None:
    question = "Fetch https://docs.python.org/3.13/whatsnew/3.13.html and summarise it."
    evidence = [
        _fetch(
            1,
            "https://docs.python.org/3.13/other.html",
            "Python 3.13 release notes explain another page. " * 4,
        )
    ]

    assessment = assess(question, evidence)

    assert assessment.ok is False
    assert any("https://docs.python.org/3.13/whatsnew/3.13.html" in issue
               for issue in assessment.issues)


def test_local_document_question_can_still_use_rag() -> None:
    question = "According to the supplied handbook, what is configuration management?"
    assessment = assess(question, [
        _evidence(
            "doc_1", "rag", "nasa_Systems_handbook.pdf#p1-chunk-2",
            "Configuration management tracks and adjudicates change requests " * 4,
        )
    ])

    assert assessment.ok is True


def test_writer_gate_does_not_call_the_model_when_evidence_is_insufficient(tmp_path) -> None:
    board = Blackboard.create(Task(question=PYTHON_QUESTION), store=SQLiteStore(tmp_path / "bb.db"))
    board.append(
        "evidence",
        _evidence("doc_1", "rag", "nasa_Systems_handbook.pdf#p1", "configuration management"),
        author="researcher",
    )
    called = []

    def generate(_prompt):
        called.append(True)
        return FinalReport(answer="unsupported", confidence=1.0, run_id=board.run_id)

    ack = write_report(board.run_id, "write", store=board.store, generate=generate)
    report, _ = board.read("final_report")

    assert called == []
    assert ack.get("insufficient") is True
    assert report.confidence == 0.0
    assert "does not satisfy" in report.answer
    assert report.citations == []


def test_writer_answers_from_a_successful_fetch_when_search_failed(
    tmp_path, monkeypatch
) -> None:
    question = "What changed in Python 3.13?"
    board = Blackboard.create(Task(question=question), store=SQLiteStore(tmp_path / "bb.db"))
    board.append(
        "evidence",
        _fetch(
            1,
            "https://docs.python.org/3.13/whatsnew/3.13.html",
            "Python 3.13 release notes explain the changed behavior. " * 4,
        ),
        author="web_researcher",
    )
    monkeypatch.setattr(
        "athena.agents.crew_writer.build_tool_report",
        lambda *_args, **_kwargs: {
            "enabled": ["rag_search", "web_search", "fetch_page", "calculate", "forecast"],
            "planned": ["web_search", "fetch_page"],
            "invoked": ["web_search", "fetch_page"],
            "failed": ["web_search"],
            "usable_evidence": ["fetch_page"],
            "calls": [
                {"tool": "web_search", "agent": "web_researcher", "ok": False,
                 "error": "provider throttled", "blocked_by": ""},
                {"tool": "fetch_page", "agent": "web_researcher", "ok": True,
                 "error": "", "blocked_by": "", "result_status": "ok"},
            ],
            "assignments": {"web_researcher": ["web_search", "fetch_page"]},
        },
    )
    called = []

    def generate(prompt):
        called.append(prompt)
        return FinalReport(
            answer="Python 3.13 changed behavior documented in the fetched release notes.",
            key_points=["A documented change is supported by the fetched page."],
            citations=[],
            confidence=0.6,
            run_id=board.run_id,
        )

    ack = write_report(board.run_id, "write", store=board.store, generate=generate)

    assert called, "the writer must use the successful fetched evidence"
    assert not ack.get("insufficient")
    assert "provider throttled" in called[0]
    assert "non-blocking" in called[0].lower()


def test_writer_produces_a_qualified_partial_answer_from_relevant_snippets(
    tmp_path, monkeypatch
) -> None:
    question = (
        "How does SpaceX's nitrogen thruster work in space? "
        "What's the working principle behind it?"
    )
    board = Blackboard.create(Task(question=question), store=SQLiteStore(tmp_path / "bb.db"))
    for index, content in enumerate([
        "A cold gas thruster expands pressurized nitrogen through a nozzle to produce thrust without combustion.",
        "A nitrogen cold gas thruster stores gas under pressure and releases it through valves and nozzles.",
        "Cold gas thrusters provide low thrust for spacecraft attitude control.",
    ], start=1):
        board.append(
            "evidence",
            _evidence(
                f"web-{index}", "web", f"https://example.com/source-{index}",
                content + " " + content,
            ),
            author="web_researcher",
        )
    monkeypatch.setattr(
        "athena.agents.crew_writer.build_tool_report",
        lambda *_args, **_kwargs: {
            "enabled": ["web_search", "fetch_page"],
            "planned": ["web_search", "fetch_page"],
            "invoked": ["web_search", "fetch_page"],
            "failed": ["fetch_page"],
            "usable_evidence": ["web_search"],
            "calls": [
                {"tool": "web_search", "agent": "web_researcher", "ok": True,
                 "error": "", "blocked_by": "", "sources": [{"url": "https://example.com"}]},
                {"tool": "fetch_page", "agent": "web_researcher", "ok": False,
                 "error": "HTTP 403", "blocked_by": ""},
            ],
            "assignments": {"web_researcher": ["web_search", "fetch_page"]},
        },
    )
    called = []

    def generate(prompt):
        called.append(prompt)
        return FinalReport(
            answer=(
                "A nitrogen thruster is a cold-gas thruster: pressurized nitrogen "
                "expands through a valve and nozzle to produce thrust without "
                "combustion. No retrieved page was read, so SpaceX-specific hardware "
                "details remain unverified."
            ),
            key_points=["The general cold-gas principle is supported by search snippets."],
            citations=[],
            confidence=0.5,
            run_id=board.run_id,
        )

    ack = write_report(board.run_id, "write", store=board.store, generate=generate)
    report, _ = board.read("final_report")

    assert called, "partial mode must call the writer"
    assert ack.get("partial") is True
    assert "No retrieved page was read" in report.answer
    assert "PARTIAL ANSWER MODE" in called[0]
    assert "search snippets" in called[0].lower()


def test_writer_gate_allows_a_relevant_local_document(tmp_path) -> None:
    question = "According to the supplied handbook, what is configuration management?"
    board = Blackboard.create(Task(question=question), store=SQLiteStore(tmp_path / "bb.db"))
    board.append(
        "evidence",
        _evidence(
            "doc_1", "rag", "nasa_Systems_handbook.pdf#p1",
            "Configuration management tracks change requests and approvals. " * 4,
        ),
        author="researcher",
    )
    called = []

    def generate(_prompt):
        called.append(True)
        return FinalReport(
            answer="Configuration management controls changes and approvals.",
            key_points=["It tracks changes."],
            citations=[], confidence=0.7, run_id=board.run_id,
        )

    ack = write_report(board.run_id, "write", store=board.store, generate=generate)

    assert called == [True]
    assert not ack.get("insufficient")
