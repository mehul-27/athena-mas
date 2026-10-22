"""Offline tests for the blackboard schemas and store semantics.

Every test gets its own SQLite file under pytest's ``tmp_path``. No network, no
LLM, no agents. Concurrency tests live in ``test_blackboard_concurrency.py``.
"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from athena.blackboard import (
    Blackboard,
    Citation,
    Draft,
    EvidenceItem,
    FinalReport,
    GuardrailFlag,
    Plan,
    PlanStep,
    SQLiteStore,
    Task,
)
from athena.blackboard.store import (
    SectionTypeError,
    UnknownSectionError,
    VersionConflictError,
)


@pytest.fixture()
def store(tmp_path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "blackboard.db")


@pytest.fixture()
def bb(store: SQLiteStore) -> Blackboard:
    return Blackboard.create(Task(question="Why is the number 42 famous?"), store=store)


def _plan() -> Plan:
    return Plan(
        steps=[PlanStep(id="s1", description="Collect sources", assigned_to="researcher")]
    )


def _evidence() -> EvidenceItem:
    return EvidenceItem(
        id="e1",
        source_type="web",
        source_ref="https://example.com/42",
        content="42 is the answer to everything.",
        retrieved_by="researcher",
    )


def test_create_write_read_roundtrip(bb: Blackboard) -> None:
    task, task_version = bb.read("task")
    assert isinstance(task, Task)
    assert task.run_id == bb.run_id
    assert task_version == 1

    plan = _plan()
    assert bb.write("plan", plan, author="planner") == 1
    value, version = bb.read("plan")
    assert value == plan
    assert version == 1

    assert bb.write("plan", plan.model_copy(update={"steps": []}), author="planner") == 2
    value, version = bb.read("plan")
    assert value.steps == []
    assert version == 2


def test_unknown_section_rejected(bb: Blackboard) -> None:
    with pytest.raises(UnknownSectionError):
        bb.write("notes", Draft(text="not a section"), author="writer")
    with pytest.raises(UnknownSectionError):
        bb.read("notes")


def test_wrong_type_rejected(bb: Blackboard) -> None:
    with pytest.raises(SectionTypeError):
        bb.write("plan", Draft(text="a draft is not a plan"), author="writer")
    with pytest.raises(SectionTypeError):
        bb.append("plan", PlanStep(id="s2", description="x", assigned_to="y"), author="z")
    with pytest.raises(SectionTypeError):
        bb.append("evidence", Draft(text="a draft is not evidence"), author="researcher")


def test_read_validates_corrupted_row(store: SQLiteStore, bb: Blackboard) -> None:
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            "UPDATE sections SET value_json = ? WHERE run_id = ? AND section = 'task'",
            ('{"not_a_task": true}', bb.run_id),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SectionTypeError):
        bb.read("task")


def test_expected_version_conflict(bb: Blackboard) -> None:
    assert bb.write("draft", Draft(text="first"), author="writer") == 1

    with pytest.raises(VersionConflictError):
        bb.write("draft", Draft(text="stale"), author="writer", expected_version=0)

    assert bb.write("draft", Draft(text="second"), author="writer", expected_version=1) == 2
    assert bb.read("draft")[0].text == "second"


def test_history_is_complete_and_ordered(bb: Blackboard) -> None:
    evidence = _evidence()
    bb.write("plan", _plan(), author="planner")
    bb.append("evidence", evidence, author="researcher")
    bb.write("draft", Draft(text="draft"), author="writer")

    events = bb.history()
    assert [event["operation"] for event in events] == [
        "create",
        "write",
        "append",
        "write",
    ]
    assert [event["section"] for event in events] == [
        "task",
        "plan",
        "evidence",
        "draft",
    ]
    assert [event["author"] for event in events] == [
        "system",
        "planner",
        "researcher",
        "writer",
    ]
    assert [event["version"] for event in events] == [1, 1, 1, 1]
    assert [event["seq"] for event in events] == sorted(
        event["seq"] for event in events
    )
    assert [event["timestamp"] for event in events] == sorted(
        event["timestamp"] for event in events
    )
    # An append records the appended item, not the whole accumulating list.
    assert events[2]["value"]["id"] == evidence.id


def test_history_is_append_only(store: SQLiteStore, bb: Blackboard) -> None:
    conn = sqlite3.connect(store.db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE history SET author = 'mallory'")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM history")
    finally:
        conn.close()


def test_final_report_markdown() -> None:
    report = FinalReport(
        answer="42 is famous because of Douglas Adams.",
        key_points=["It is the answer to everything.", "It became a meme."],
        citations=[
            Citation(
                source_type="web",
                source_ref="https://example.com/42",
                quote_or_summary="The answer is 42.",
            ),
            Citation(
                source_type="rag",
                source_ref="doc-17",
                quote_or_summary="Adams chose the number arbitrarily.",
            ),
        ],
        confidence=0.8,
        tools_used=["web_search"],
        guardrail_flags=[
            GuardrailFlag(
                stage="final_output",
                rule="no_pii",
                action="warned",
                detail="No personal data found.",
            )
        ],
        run_id="run-1",
    )

    markdown = report.to_markdown()
    assert report.answer in markdown
    for point in report.key_points:
        assert point in markdown
    for citation in report.citations:
        assert citation.source_ref in markdown
        assert citation.quote_or_summary in markdown
    assert "| # | Source type | Source | Quote or summary |" in markdown


def test_final_report_rejects_confidence_out_of_range() -> None:
    base = {"answer": "x", "run_id": "run-1"}
    with pytest.raises(ValidationError):
        FinalReport(**base, confidence=1.5)
    with pytest.raises(ValidationError):
        FinalReport(**base, confidence=-0.1)
