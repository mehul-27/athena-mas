"""Secret-shaped report output is flagged and scrubbed before SQLite writes."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from athena.agents import crew_writer, report_sanitize
from athena.blackboard import (
    Blackboard,
    Citation,
    EvidenceItem,
    FinalReport,
    GuardrailFlag,
    SQLiteStore,
    Task,
)
from athena.patterns import p2p

CANARY = "gsk_testCanary_1234567890ABC"


def _raw_section(store: SQLiteStore, run_id: str, name: str) -> str:
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT value_json FROM sections WHERE run_id=? AND section=?",
            (run_id, name),
        ).fetchone()
    assert row is not None
    return row[0]


def test_report_scrub_preserves_benign_content_and_flags_without_echoing_secret(tmp_path):
    store = SQLiteStore(tmp_path / "board.db")
    board = Blackboard.create(Task(question="Summarize this source."), store=store)
    report = FinalReport(
        answer=f"Benign answer remains. {CANARY}",
        key_points=["Useful point remains.", f"Bearer {CANARY}"],
        citations=[
            Citation(
                source_type="web",
                source_ref="https://example.test/source",
                quote_or_summary=f"Public source note. api_key={CANARY}",
            )
        ],
        confidence=0.7,
        tools_used=["web_search", CANARY],
        guardrail_flags=[
            GuardrailFlag(
                stage="final_output",
                rule="report.test",
                action="sanitized",
                detail=f"Detected {CANARY}",
            )
        ],
        run_id=board.run_id,
    )

    clean = report_sanitize.redact_final_report(report)
    board.write("final_report", clean, author="writer")
    raw = _raw_section(store, board.run_id, "final_report")
    stored = board.read("final_report")[0]
    rendered = stored.to_markdown()
    with sqlite3.connect(store.db_path) as conn:
        history_value = conn.execute(
            "SELECT value_json FROM history WHERE run_id=? AND section='final_report'",
            (board.run_id,),
        ).fetchone()[0]

    assert CANARY not in raw
    assert CANARY not in history_value
    assert CANARY not in rendered
    assert "Benign answer remains." in rendered
    assert "Useful point remains." in rendered
    assert "https://example.test/source" in rendered
    assert "web_search" in rendered
    assert any(flag.rule == "output.secret_leak" for flag in clean.guardrail_flags)
    assert all(CANARY not in flag.detail for flag in clean.guardrail_flags)


def test_evidence_backed_sanitizer_drops_secret_ref_and_keeps_safe_citation(tmp_path):
    store = SQLiteStore(tmp_path / "board.db")
    board = Blackboard.create(Task(question="What is systems engineering?"), store=store)
    safe_ref = "handbook.pdf#p11"
    secret_ref = f"https://example.test/page?api_key={CANARY}"
    items = [
        EvidenceItem(
            id="safe-doc", source_type="rag", source_ref=safe_ref,
            content="Systems engineering is a methodical approach to design. " * 4,
            retrieved_by="researcher",
        ),
        EvidenceItem(
            id="unsafe-ref", source_type="web", source_ref=secret_ref,
            content="Systems engineering is a methodical approach to design. " * 4,
            retrieved_by="researcher",
        ),
    ]
    for item in items:
        board.append("evidence", item, author="researcher")
    report = FinalReport(
        answer="Systems engineering is a methodical approach to design.",
        key_points=["The approach is methodical."],
        citations=[
            Citation(source_type="rag", source_ref=safe_ref, quote_or_summary="methodical"),
            Citation(source_type="web", source_ref=secret_ref, quote_or_summary="secret"),
        ],
        confidence=0.7,
        run_id=board.run_id,
    )

    clean = report_sanitize.sanitize_report(
        report, board, board.run_id, items=items, question="What is systems engineering?"
    )
    board.write("final_report", clean, author="writer")
    raw = _raw_section(store, board.run_id, "final_report")

    assert CANARY not in raw
    assert clean.answer.startswith("Systems engineering is a methodical approach")
    assert [citation.source_ref for citation in clean.citations] == [safe_ref]
    assert "report.secret_citation" in {flag.rule for flag in clean.guardrail_flags}


def test_normal_writer_scrubs_supported_secret_before_draft_and_final_persistence(tmp_path):
    store = SQLiteStore(tmp_path / "board.db")
    board = Blackboard.create(Task(question="What is systems engineering?"), store=store)
    source_ref = "handbook.pdf#p11-chunk-28"
    content = (
        "NASA defines systems engineering as a methodical approach. "
        f"The synthetic example includes marker {CANARY}. "
    ) * 3
    board.append(
        "evidence",
        EvidenceItem(
            id="doc-1", source_type="rag", source_ref=source_ref,
            content=content, retrieved_by="researcher",
        ),
        author="researcher",
    )

    def generate(_prompt: str) -> FinalReport:
        return FinalReport(
            answer=(
                "NASA defines systems engineering as a methodical approach. "
                f"The synthetic example includes marker {CANARY}."
            ),
            key_points=[f"Synthetic marker: {CANARY}."],
            citations=[
                Citation(
                    source_type="rag", source_ref=source_ref,
                    quote_or_summary=f"Source quote includes {CANARY}.",
                )
            ],
            confidence=0.7,
            run_id=board.run_id,
        )

    crew_writer.write_report(board.run_id, "write", store=store, generate=generate)
    report, _ = board.read("final_report")
    draft, _ = board.read("draft")

    assert CANARY not in _raw_section(store, board.run_id, "final_report")
    assert CANARY not in _raw_section(store, board.run_id, "draft")
    assert CANARY not in draft.text
    assert report.citations[0].source_ref == source_ref
    assert any(flag.rule == "output.secret_leak" for flag in report.guardrail_flags)
    assert all(CANARY not in flag.detail for flag in report.guardrail_flags)


def test_insufficient_evidence_writer_scrubs_draft_and_final_report(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "board.db")
    board = Blackboard.create(Task(question="What is established?"), store=store)
    assessment = SimpleNamespace(
        ok=False,
        issues=[f"Evidence missing ({CANARY})"],
        limitations=[],
        confidence_ceiling=0.2,
        required=1,
    )
    monkeypatch.setattr(crew_writer, "assess", lambda *_args, **_kwargs: assessment)
    monkeypatch.setattr(crew_writer, "partial_snippet_evidence", lambda *_args: False)

    crew_writer.write_report(board.run_id, "write", store=store)

    for name in ("draft", "final_report"):
        assert CANARY not in _raw_section(store, board.run_id, name)
    report, _ = board.read("final_report")
    assert "The available evidence does not satisfy" in report.answer
    assert any(flag.rule == "output.secret_leak" for flag in report.guardrail_flags)
    assert all(CANARY not in flag.detail for flag in report.guardrail_flags)


def test_p2p_insufficient_evidence_and_writer_failure_scrub_before_write(
    tmp_path, monkeypatch
):
    store = SQLiteStore(tmp_path / "board.db")
    board = Blackboard.create(Task(question="What is established?"), store=store)
    agent = p2p.P2PAgent(
        name="supervisor", run_id=board.run_id, question="What is established?", store=store
    )
    assessment = SimpleNamespace(
        ok=False,
        issues=[f"Evidence missing ({CANARY})"],
        limitations=[],
        confidence_ceiling=0.2,
        required=1,
        search_only=False,
    )
    monkeypatch.setattr(p2p, "assess", lambda *_args, **_kwargs: assessment)
    monkeypatch.setattr(p2p, "partial_snippet_evidence", lambda *_args: False)

    agent._audit_report("{}", items=[])
    for name in ("draft", "final_report"):
        assert CANARY not in _raw_section(store, board.run_id, name)
    report, _ = board.read("final_report")
    assert any(flag.rule == "output.secret_leak" for flag in report.guardrail_flags)

    agent._audit_failure("writer", f"writer exception carried {CANARY}")
    assert CANARY not in _raw_section(store, board.run_id, "final_report")
    assert CANARY not in agent._blackboard().read("final_report")[0].to_markdown()
