"""Offline tests for bb_io rendering and the evidence_recorder hook."""

from __future__ import annotations

import pytest

from athena.agents import bb_io
from athena.agents.evidence_recorder import record_tool_result
from athena.agents.untrusted import UNTRUSTED_BEGIN, UNTRUSTED_END
from athena.blackboard import Blackboard, EvidenceItem, SQLiteStore, Task
from athena.tools.schemas import CalcResult, RagHit, RagResult, WebHit, WebResult


@pytest.fixture()
def store(tmp_path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "bb.db")


@pytest.fixture()
def bb(store: SQLiteStore) -> Blackboard:
    return Blackboard.create(Task(question="Why is the sky blue?"), store=store)


def _evidence(index: int, content: str | None = None) -> EvidenceItem:
    return EvidenceItem(
        id=f"e{index}",
        source_type="rag",
        source_ref=f"handbook.pdf#p{index}-chunk-{index}",
        content=content or "short passage",
        retrieved_by="researcher",
    )


def test_evidence_text_bounds_items_and_truncates(bb: Blackboard) -> None:
    for index in range(12):
        bb.append("evidence", _evidence(index, "word " * 400), author="researcher")

    text = bb_io.evidence_text(bb)

    lines = text.splitlines()
    item_lines = [line for line in lines if line.startswith("[")]
    assert lines[0] == UNTRUSTED_BEGIN
    assert lines[-1] == UNTRUSTED_END
    assert len(item_lines) == bb_io.MAX_EVIDENCE_ITEMS
    assert all(line.endswith("...") for line in item_lines)
    assert all(len(line) < bb_io.MAX_EVIDENCE_CHARS + 120 for line in item_lines)
    assert "handbook.pdf#p0-chunk-0" in text


def test_evidence_text_respects_a_custom_limit(bb: Blackboard) -> None:
    for index in range(5):
        bb.append("evidence", _evidence(index), author="researcher")
    text = bb_io.evidence_text(bb, limit=2)
    item_lines = [line for line in text.splitlines() if line.startswith("[")]

    assert text.startswith(UNTRUSTED_BEGIN)
    assert text.endswith(UNTRUSTED_END)
    assert len(item_lines) == 2


def test_empty_sections_render_placeholders(bb: Blackboard) -> None:
    assert bb_io.evidence_text(bb) == "(no evidence recorded)"
    assert bb_io.plan_text(bb) == "(no plan recorded)"
    assert bb_io.analysis_text(bb) == "(no analysis yet)"
    assert bb_io.draft_text(bb) == "(no draft yet)"
    assert bb_io.report_text(bb) == "(no final report yet)"
    assert "Why is the sky blue?" in bb_io.task_text(bb)


def test_recorder_appends_rag_evidence(bb: Blackboard, store: SQLiteStore) -> None:
    response = {
        "content": [],
        "isError": False,
        "structuredContent": RagResult(
            query="q",
            top_k=1,
            elapsed_ms=1.0,
            hits=[
                RagHit(
                    chunk_id="doc_1",
                    document_id="d1",
                    document="handbook.pdf",
                    chunk_index=28,
                    page=11,
                    text="NASA defines systems engineering...",
                    score=0.64,
                )
            ],
        ).model_dump(),
    }

    written = record_tool_result(bb.run_id, "rag_search", response, store)

    items, _ = bb.read("evidence")
    assert written == 1
    assert items[0].id == "doc_1"
    assert items[0].source_ref == "handbook.pdf#p11-chunk-28"
    assert items[0].source_type == "rag"
    assert items[0].retrieved_by == "researcher"
    assert bb.history()[-1]["author"] == "researcher"


def test_recorder_handles_web_and_calc(bb: Blackboard, store: SQLiteStore) -> None:
    web = WebResult(
        query="q", provider="duckduckgo", max_results=1, elapsed_ms=1.0,
        results=[WebHit(title="NASA", url="https://nasa.gov", snippet="The agency.")],
    )
    calc = CalcResult(expression="2+3", value=5.0, elapsed_ms=0.1)

    record_tool_result(bb.run_id, "web_search", {"structuredContent": web.model_dump()}, store)
    record_tool_result(bb.run_id, "calculate", {"structuredContent": calc.model_dump()}, store)

    items, _ = bb.read("evidence")
    assert [item.source_type for item in items] == ["web", "calculation"]
    assert items[0].source_ref == "https://nasa.gov"
    assert "5" in items[1].content


def test_recorder_skips_failed_tools(bb: Blackboard, store: SQLiteStore) -> None:
    failed = RagResult(query="q", top_k=3, elapsed_ms=1.0, ok=False, error="index missing")
    errored = {"isError": True, "structuredContent": {"anything": 1}}

    assert record_tool_result(bb.run_id, "rag_search", {"structuredContent": failed.model_dump()}, store) == 0
    assert record_tool_result(bb.run_id, "rag_search", errored, store) == 0
    assert bb.read("evidence") is None


def test_recorder_ignores_unknown_tools(bb: Blackboard, store: SQLiteStore) -> None:
    assert record_tool_result(bb.run_id, "delete_everything", {"ok": True}, store) == 0
