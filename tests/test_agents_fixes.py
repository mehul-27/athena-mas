"""Offline tests for the Step 5 fixes: truncation, plan roles, clean text, prompts."""

from __future__ import annotations

import pytest

from athena.agents.crew_roles import analyse, critique
from athena.agents.crew_writer import looks_truncated, write_report, writer_prompt
from athena.agents.planner import WORKER_ROLES, normalise_role, parse_plan_text, parse_steps
from athena.agents.untrusted import UNTRUSTED_DATA_RULE
from athena.blackboard import Blackboard, EvidenceItem, FinalReport, SQLiteStore, Task
from athena.tools.adapters import clean_text, to_evidence_items
from athena.tools.schemas import RagHit, RagResult


@pytest.fixture()
def bb(tmp_path):
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="What is systems engineering?"), store=store)
    blackboard.append(
        "evidence",
        EvidenceItem(
            id="doc_1", source_type="rag", source_ref="handbook.pdf#p11-chunk-28",
            content="NASA defines systems engineering as a methodical approach.",
            score=0.64, retrieved_by="researcher",
        ),
        author="researcher",
    )
    return blackboard


def _report(answer: str, confidence: float = 0.8) -> FinalReport:
    return FinalReport(
        answer=answer, key_points=[], citations=[], confidence=confidence,
        tools_used=[], run_id="ignored",
    )


def test_truncation_is_detected() -> None:
    assert looks_truncated("This sentence just stops")
    assert looks_truncated("")
    assert not looks_truncated("This sentence is finished.")
    assert not looks_truncated('He said "done."')


def test_truncated_answer_is_retried_once(bb: Blackboard) -> None:
    calls = []

    def generate(prompt: str) -> FinalReport:
        calls.append(prompt)
        return _report("Cut off mid senten" if len(calls) == 1 else "A complete answer.")

    ack = write_report(bb.run_id, "write", store=bb.store, generate=generate)

    assert len(calls) == 2
    assert "cut off" in calls[1].lower()
    assert ack.get("truncated") is not True
    assert bb.read("final_report")[0].answer == "A complete answer."


def test_still_truncated_answer_is_marked(bb: Blackboard) -> None:
    def generate(prompt: str) -> FinalReport:
        return _report("Always cut off mid senten", confidence=0.9)

    ack = write_report(bb.run_id, "write", store=bb.store, generate=generate)

    report, _ = bb.read("final_report")
    assert ack.get("truncated") is True
    assert report.answer.endswith("[truncated]")
    assert report.confidence <= 0.5


def test_plan_roles_are_normalised() -> None:
    assert normalise_role("rag_researcher") == "rag_researcher"
    assert normalise_role("Web Researcher") == "web_researcher"
    assert normalise_role("calc-researcher") == "calc_researcher"
    assert normalise_role("researcher") == "rag_researcher"
    assert normalise_role("nonsense") in WORKER_ROLES

    steps = parse_steps(["researcher: look it up", "web_researcher: check the news"])
    assert [step.assigned_to for step in steps] == ["rag_researcher", "web_researcher"]


def test_plan_text_parsing_bounds_the_steps() -> None:
    raw = '{"steps": ["rag_researcher: a", "web_researcher: b", "calc_researcher: c", "extra: d", "extra: e"]}'
    steps = parse_plan_text(raw)
    assert len(steps) == 4
    assert all(step.assigned_to in WORKER_ROLES for step in steps)


def test_replacement_character_becomes_a_hyphen() -> None:
    assert clean_text("procedures\ufffdthe rest") == "procedures-the rest"

    result = RagResult(
        query="q", top_k=1, elapsed_ms=1.0,
        hits=[RagHit(chunk_id="c1", document_id="d", document="doc.pdf",
                     chunk_index=1, text="procedures\ufffdthe rest", score=0.5)],
    )
    items = to_evidence_items(result, retrieved_by="researcher")
    assert "\ufffd" not in items[0].content
    assert "procedures-the rest" in items[0].content


def test_analyst_and_critic_prompts_carry_the_rules(bb: Blackboard) -> None:
    seen = {}

    def capture_analyst(prompt: str):
        seen["analyst"] = prompt
        return __import__("athena.blackboard", fromlist=["Analysis"]).Analysis(
            key_findings=[], gaps=[]
        )

    analyse(bb.run_id, "go", store=bb.store, generate=capture_analyst)
    assert "never by internal ids" in seen["analyst"].lower() or "internal id" in seen["analyst"].lower()
    assert "source_ref" in seen["analyst"]
    assert UNTRUSTED_DATA_RULE in seen["analyst"]

    def capture_critic(prompt: str):
        seen["critic"] = prompt
        return __import__("athena.blackboard", fromlist=["Critique"]).Critique(
            issues=[], severity="low", approved=True
        )

    critique(bb.run_id, "go", store=bb.store, generate=capture_critic)
    lowered = seen["critic"].lower()
    assert "approved=true" in lowered
    assert "severity is 'high' only" in lowered
    assert "list each one in issues" in lowered
    assert UNTRUSTED_DATA_RULE in seen["critic"]

    assert "150 words" in writer_prompt(bb, "go")
    assert UNTRUSTED_DATA_RULE in writer_prompt(bb, "go")
