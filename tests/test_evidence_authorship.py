"""Evidence authorship and p2p failure honesty."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from athena.agents.evidence_recorder import make_evidence_recorder
from athena.blackboard import Blackboard, SQLiteStore, Task
from athena.observability.store import SQLiteEventStore
from athena.summary import RunSummary, finish, note_ack


@pytest.fixture()
def blackboard(tmp_path) -> Blackboard:
    return Blackboard.create(Task(question="q"), store=SQLiteStore(tmp_path / "bb.db"))


def _tool_response():
    """The shape the MCP rag_search tool returns."""
    payload = {
        "tool_name": "rag_search", "elapsed_ms": 12.0, "ok": True, "error": None,
        "query": "nasa", "top_k": 3,
        "hits": [{
            "chunk_id": "c1", "document_id": "nasa", "document": "nasa handbook",
            "chunk_index": 1, "page": 1,
            "text": "Systems engineering is a discipline.", "score": 0.9,
        }],
    }
    return {"structuredContent": payload, "isError": False}


def test_evidence_is_attributed_to_the_calling_worker(blackboard: Blackboard) -> None:
    recorder = make_evidence_recorder(blackboard.run_id, blackboard.store)
    recorder(SimpleNamespace(name="rag_search"), {"query": "nasa"},
             SimpleNamespace(agent_name="rag_researcher"), _tool_response())

    items = blackboard.read("evidence")
    assert items, "evidence must be recorded"
    authors = {item.retrieved_by for item in items[0]}
    assert authors == {"rag_researcher"}, "the worker owns what it retrieved"
    assert "researcher" not in authors


def test_the_same_chunk_is_not_duplicated(blackboard: Blackboard) -> None:
    recorder = make_evidence_recorder(blackboard.run_id, blackboard.store)
    for _ in range(3):
        recorder(SimpleNamespace(name="rag_search"), {"query": "nasa"},
                 SimpleNamespace(agent_name="rag_researcher"), _tool_response())

    assert len(blackboard.read("evidence")[0]) == 1


def test_blackboard_write_event_names_the_worker(blackboard: Blackboard) -> None:
    from athena.observability.hooks import install_hooks
    from athena.observability.recorder import Recorder, set_recorder

    install_hooks()
    set_recorder(Recorder(blackboard.run_id, SQLiteEventStore()))
    try:
        recorder = make_evidence_recorder(blackboard.run_id, blackboard.store)
        recorder(SimpleNamespace(name="rag_search"), {"query": "nasa"},
                 SimpleNamespace(agent_name="rag_researcher"), _tool_response())
    finally:
        set_recorder(None)

    events = SQLiteEventStore().events(blackboard.run_id)
    writes = [event for event in events if event.kind == "blackboard_write"]
    assert writes, "the write must be visible to monitoring"
    assert writes[-1].agent == "rag_researcher"


def test_p2p_failed_role_does_not_write_and_degrades(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=store)
    ack = json.dumps({"ok": False, "role": "analyst", "error_type": "TypeError",
                      "error": "analyse() got an unexpected keyword argument"})

    summary = RunSummary(pattern="p2p")
    note_ack(summary, ack)
    finish(summary)

    assert summary.degraded is True
    assert summary.stage_errors == ["analyst: TypeError"]
    assert blackboard.read("analysis") is None, "no placeholder may be written"
