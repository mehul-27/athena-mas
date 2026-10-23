"""Offline tests for the writer role: retry, fallback, and citation hygiene."""

from __future__ import annotations

import pytest

from athena.agents.crew_writer import write_report
from athena.blackboard import (
    Blackboard,
    Citation,
    EvidenceItem,
    FinalReport,
    SQLiteStore,
    Task,
)


@pytest.fixture()
def store(tmp_path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "bb.db")


@pytest.fixture()
def bb(store: SQLiteStore) -> Blackboard:
    blackboard = Blackboard.create(Task(question="What is systems engineering?"), store=store)
    blackboard.append(
        "evidence",
        EvidenceItem(
            id="doc_1",
            source_type="rag",
            source_ref="handbook.pdf#p11-chunk-28",
            content="NASA defines systems engineering as a methodical approach.",
            score=0.64,
            retrieved_by="researcher",
        ),
        author="researcher",
    )
    return blackboard


def test_writer_fallback_after_two_invalid_generations(bb: Blackboard, store: SQLiteStore) -> None:
    calls = []

    def invalid(prompt: str):
        calls.append(prompt)
        raise ValueError("output did not match FinalReport")

    ack = write_report(bb.run_id, "write the report", store=store, generate=invalid)

    assert ack["fallback"] is True
    assert len(calls) == 2  # first attempt + one retry carrying the error
    assert "output did not match FinalReport" in calls[1]

    report, _ = bb.read("final_report")
    assert report.confidence == 0.0
    assert report.run_id == bb.run_id
    assert "could not be generated" in report.answer
    assert bb.read("draft") is not None
    assert bb.history()[-1]["author"] == "writer"


def test_writer_drops_invented_citations_and_derives_tools(bb: Blackboard, store: SQLiteStore) -> None:
    def generate(prompt: str) -> FinalReport:
        return FinalReport(
            answer="Systems engineering is methodical.",
            key_points=["It is multidisciplinary."],
            citations=[
                Citation(source_type="web", source_ref="https://invented.example", quote_or_summary="made up"),
                Citation(source_type="web", source_ref="handbook.pdf#p11-chunk-28", quote_or_summary="real"),
            ],
            confidence=0.7,
            tools_used=["a_tool_that_never_ran"],
            guardrail_flags=[],
            run_id="wrong-run",
        )

    ack = write_report(bb.run_id, "write", store=store, generate=generate)

    assert ack.get("fallback") is not True
    report, _ = bb.read("final_report")
    assert [citation.source_ref for citation in report.citations] == ["handbook.pdf#p11-chunk-28"]
    assert report.citations[0].source_type == "rag"
    assert report.tools_used == ["rag_search"]
    assert report.run_id == bb.run_id
    assert bb.read("draft")[0].based_on_evidence_ids == ["doc_1"]


def test_writer_invents_a_citation_when_the_model_gives_none(bb: Blackboard, store: SQLiteStore) -> None:
    def generate(prompt: str) -> FinalReport:
        return FinalReport(
            answer="Answer without sources.", key_points=[], citations=[],
            confidence=0.4, tools_used=[], run_id="whatever",
        )

    write_report(bb.run_id, "write", store=store, generate=generate)

    report, _ = bb.read("final_report")
    assert report.citations, "the recorded evidence should be cited"
    assert report.citations[0].source_ref == "handbook.pdf#p11-chunk-28"
    assert "systems engineering" in report.citations[0].quote_or_summary.lower()
