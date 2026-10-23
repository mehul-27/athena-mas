"""Offline tests for the A2A contract and the role dispatcher.

The CrewAI agents are replaced by stubs: these tests check the wiring and the
guarantees (short payload, right section, right author, safe errors), not prose.
"""

from __future__ import annotations

import json

import pytest

from athena.agents.a2a_client import MAX_PAYLOAD_BYTES, build_payload
from athena.agents.crew_server import run_role_request
from athena.blackboard import Analysis, Blackboard, EvidenceItem, SQLiteStore, Task


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


def test_payload_carries_only_the_three_fields(bb: Blackboard) -> None:
    # Even with a large board, the message must stay tiny: no evidence travels.
    for index in range(20):
        bb.append(
            "evidence",
            EvidenceItem(
                id=f"e{index}", source_type="rag", source_ref=f"d#{index}",
                content="x" * 900, retrieved_by="researcher",
            ),
            author="researcher",
        )

    payload = build_payload(bb.run_id, "writer", "y" * 5000)

    assert len(payload.encode("utf-8")) < MAX_PAYLOAD_BYTES
    body = json.loads(payload)
    assert set(body) == {"run_id", "role", "instruction"}
    assert body["run_id"] == bb.run_id
    assert body["role"] == "writer"
    assert len(body["instruction"]) == 300
    assert "x" * 10 not in payload


def test_dispatch_runs_a_role_and_returns_a_short_ack(bb: Blackboard, store: SQLiteStore) -> None:
    seen = {}

    def stub_analyst(run_id, instruction, store=None):
        seen["instruction"] = instruction
        Blackboard.open(run_id, store).write(
            "analysis", Analysis(key_findings=["SE is methodical"], gaps=[]), author="analyst"
        )
        return {"section": "analysis", "version": 1}

    ack_text = run_role_request(
        json.dumps({"run_id": bb.run_id, "role": "analyst", "instruction": "go"}),
        roles={"analyst": stub_analyst},
        store=store,
    )

    ack = json.loads(ack_text)
    assert ack == {"ok": True, "role": "analyst", "section": "analysis", "version": 1}
    assert len(ack_text) < 120
    assert seen["instruction"] == "go"
    assert bb.read("analysis")[0].key_findings == ["SE is methodical"]
    assert bb.history()[-1]["author"] == "analyst"


def test_dispatch_rejects_unknown_role(bb: Blackboard, store: SQLiteStore) -> None:
    ack = json.loads(
        run_role_request(
            json.dumps({"run_id": bb.run_id, "role": "hacker", "instruction": "x"}),
            roles={"analyst": lambda *a, **k: None},
            store=store,
        )
    )
    assert ack["ok"] is False
    assert "unknown role" in ack["error"]


def test_dispatch_rejects_unknown_run(store: SQLiteStore) -> None:
    ack = json.loads(
        run_role_request(
            json.dumps({"run_id": "does-not-exist", "role": "analyst", "instruction": "x"}),
            roles={"analyst": lambda *a, **k: None},
            store=store,
        )
    )
    assert ack["ok"] is False
    assert "unknown run_id" in ack["error"]


def test_dispatch_rejects_malformed_body(store: SQLiteStore) -> None:
    ack = json.loads(run_role_request("not json at all", store=store))
    assert ack["ok"] is False
    assert "JSON" in ack["error"]


def test_dispatch_reports_a_raising_role_as_an_ack(bb: Blackboard, store: SQLiteStore) -> None:
    def broken(run_id, instruction, store=None):
        raise RuntimeError("model exploded")

    ack = json.loads(
        run_role_request(
            json.dumps({"run_id": bb.run_id, "role": "writer", "instruction": "x"}),
            roles={"writer": broken},
            store=store,
        )
    )
    assert ack["ok"] is False
    assert "model exploded" in ack["error"]
