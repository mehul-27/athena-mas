"""The critic receives the complete structured report and writer count rules."""

from __future__ import annotations

import pytest

from athena.agents import crew_roles
from athena.agents.crew_writer import writer_prompt
from athena.blackboard import (
    Blackboard,
    Citation,
    Critique,
    Draft,
    EvidenceItem,
    FinalReport,
    SQLiteStore,
    Task,
)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("List two important findings with sources.", "exactly 2 key points/findings"),
        ("List 5 findings with sources.", "exactly 5 key points/findings"),
        ("Summarize the release.", "3-5 key points and 1-3 citations"),
    ],
)
def test_writer_prompt_matches_requested_finding_count(
    tmp_path, question: str, expected: str
) -> None:
    board = Blackboard.create(Task(question=question), store=SQLiteStore(tmp_path / "bb.db"))

    prompt = writer_prompt(board, "write")

    assert expected in prompt
    if question.startswith("List two") or question.startswith("List 5"):
        assert "distinct primary-source URL for each finding" in prompt


def test_real_critic_prompt_contains_complete_final_report(tmp_path) -> None:
    board = Blackboard.create(
        Task(question="What does the documented procedure say?"),
        store=SQLiteStore(tmp_path / "bb.db"),
    )
    board.append(
        "evidence",
        EvidenceItem(
            id="evidence-1",
            source_type="web",
            source_ref="https://example.test/official-guide",
            content="The official guide describes the procedure in three steps.",
            retrieved_by="researcher",
        ),
        author="researcher",
    )
    board.write(
        "draft",
        Draft(
            text="The guide describes the documented process.",
            based_on_evidence_ids=["evidence-1"],
        ),
        author="writer",
    )
    board.write(
        "final_report",
        FinalReport(
            answer="The guide describes the documented process.",
            key_points=["CRITIC_CONTEXT_KEYPOINT: the guide names three steps"],
            citations=[
                Citation(
                    source_type="web",
                    source_ref="https://example.test/official-guide",
                    quote_or_summary="CRITIC_CONTEXT_QUOTE: the guide explicitly says three steps",
                )
            ],
            confidence=0.73,
            tools_used=["web_fetch"],
            run_id=board.run_id,
        ),
        author="writer",
    )
    captured: list[str] = []

    def fake_generate(prompt: str):
        captured.append(prompt)
        return Critique(issues=[], severity="low", approved=True)

    crew_roles.critique(board.run_id, "review", store=board.store, generate=fake_generate)

    assert len(captured) == 1
    for expected in (
        "CRITIC_CONTEXT_KEYPOINT",
        "CRITIC_CONTEXT_QUOTE",
        '"confidence": 0.73',
        '"tools_used"',
    ):
        assert expected in captured[0]


def test_critic_prioritizes_cited_source_and_reports_missing_ref(tmp_path) -> None:
    board = Blackboard.create(
        Task(question="Summarize the evidence."),
        store=SQLiteStore(tmp_path / "bb.db"),
    )
    for index in range(8):
        board.append(
            "evidence",
            EvidenceItem(
                id=f"unrelated-{index}",
                source_type="rag",
                source_ref="doc://first/source",
                content=f"Unrelated evidence item {index}.",
                retrieved_by="researcher",
            ),
            author="researcher",
        )
    board.append(
        "evidence",
        EvidenceItem(
            id="cited-ninth",
            source_type="rag",
            source_ref="doc://cited/ninth",
            content="The citation selected by the report, beyond the old first-eight window.",
            retrieved_by="researcher",
        ),
        author="researcher",
    )
    board.write(
        "draft",
        Draft(text="A short summary.", based_on_evidence_ids=[]),
        author="writer",
    )
    board.write(
        "final_report",
        FinalReport(
            answer="A short summary.",
            citations=[
                Citation(
                    source_type="rag",
                    source_ref="doc://first/source",
                    quote_or_summary="First source citation.",
                ),
                Citation(
                    source_type="rag",
                    source_ref="doc://cited/ninth",
                    quote_or_summary="One relevant point.",
                ),
                Citation(
                    source_type="web",
                    source_ref="https://missing.example/source",
                    quote_or_summary="A fabricated reference for audit coverage.",
                ),
            ],
            confidence=0.6,
            run_id=board.run_id,
        ),
        author="writer",
    )
    captured: list[str] = []

    def fake_generate(prompt: str):
        captured.append(prompt)
        return Critique(issues=[], severity="low", approved=True)

    crew_roles.critique(board.run_id, "review", store=board.store, generate=fake_generate)

    prompt = captured[0]
    assert "Citation 1: recorded and excerpted" in prompt
    assert "Citation 2: recorded and excerpted" in prompt
    assert "Citation 3: missing from recorded evidence" in prompt
    assert "[unrelated-0] rag | doc://first/source" in prompt
    assert "[cited-ninth] rag | doc://cited/ninth" in prompt
    evidence_lines = [line for line in prompt.splitlines() if line.startswith("[")]
    assert len(evidence_lines) <= 8
