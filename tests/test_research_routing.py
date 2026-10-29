"""Source routing and evidence-sufficiency gate tests (offline)."""

from __future__ import annotations

from athena.agents.crew_writer import write_report
from athena.agents import researcher as researcher_module
from athena.agents.routing import (
    classify,
    ensure_step_texts,
    is_primary_source,
    is_relevant,
    source_guidance,
)
from athena.agents.sufficiency import assess
from athena.blackboard import Blackboard, EvidenceItem, FinalReport, SQLiteStore, Task

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
