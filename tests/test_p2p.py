"""Offline tests for the p2p pattern: message-carried context, audit-only writes."""

from __future__ import annotations

import json

import pytest

from athena.agents import bb_io
from athena.agents.a2a_client import build_payload
from athena.blackboard import Blackboard, SQLiteStore, Task
from athena.patterns.context import P2PContext
from athena.patterns.p2p import P2PAgent


@pytest.fixture()
def setup(tmp_path):
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="What is systems engineering?"), store=store)
    agent = P2PAgent(
        name="supervisor",
        run_id=blackboard.run_id,
        question="What is systems engineering?",
        card_url="http://127.0.0.1:9001/.well-known/agent-card.json",
        store=store,
    )
    return store, blackboard, agent


def test_p2p_payload_carries_context_and_is_larger() -> None:
    plain = build_payload("run-1", "writer", "go")
    contextual = build_payload("run-1", "writer", "go", context="EVIDENCE: " + "x" * 2000)

    assert "context" not in json.loads(plain)
    assert json.loads(contextual)["context"].startswith("EVIDENCE:")
    assert len(contextual) > len(plain) * 10


def test_p2p_context_renders_with_the_blackboard_limits() -> None:
    context = P2PContext(question="q")

    class Item:
        id = "doc_1"
        source_type = "rag"
        source_ref = "handbook.pdf#p1-chunk-2"
        content = "word " * 500

    context.evidence = [Item()]
    block = context.evidence_block()

    assert block.endswith("...")
    assert len(block) < 800


def test_p2p_agents_never_read_the_blackboard(setup) -> None:
    _store, blackboard, agent = setup
    report = {
        "answer": "Systems engineering is methodical.",
        "key_points": ["one"],
        "citations": [],
        "confidence": 0.5,
        "tools_used": [],
        "guardrail_flags": [],
        "run_id": blackboard.run_id,
    }

    bb_io.forbid_reads(True)
    try:
        agent._audit_analysis(json.dumps({"key_findings": ["a"], "gaps": []}))
        agent._audit_report(json.dumps(report), items=[])
        agent._audit_critique(json.dumps({"issues": [], "severity": "low", "approved": True}))
    finally:
        bb_io.forbid_reads(False)

    assert blackboard.read("analysis")[0].key_findings == ["a"]
    assert blackboard.read("final_report")[0].answer.startswith("Systems engineering")
    assert blackboard.read("critique")[0].approved is True


def test_bb_io_reads_raise_while_forbidden() -> None:
    bb_io.forbid_reads(True)
    try:
        with pytest.raises(bb_io.BlackboardReadForbidden):
            bb_io.task_text(None)
    finally:
        bb_io.forbid_reads(False)


def test_p2p_audit_records_keep_the_blackboard_authors(setup) -> None:
    _store, blackboard, agent = setup

    agent._audit_analysis(json.dumps({"key_findings": ["a"], "gaps": []}))
    agent._audit_critique(json.dumps({"issues": [], "severity": "low", "approved": True}))

    authors = [entry["author"] for entry in blackboard.history()]
    assert authors == ["system", "analyst", "critic"]
    sections = [entry["section"] for entry in blackboard.history()]
    assert sections == ["task", "analysis", "critique"]
