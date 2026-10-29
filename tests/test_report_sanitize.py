"""Citation safety and tool attribution for fetched evidence."""

from __future__ import annotations

from athena.agents.report_sanitize import sanitize_report
from athena.blackboard import Citation, EvidenceItem, FinalReport


def _evidence(
    evidence_id: str,
    source_type: str,
    source_ref: str,
    content: str,
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        source_type=source_type,
        source_ref=source_ref,
        content=content,
        retrieved_by="researcher",
    )


def _report(citations=None) -> FinalReport:
    return FinalReport(
        answer="Answer.",
        key_points=[],
        citations=citations or [],
        confidence=0.6,
        tools_used=[],
        run_id="r",
    )


def test_failed_and_empty_fetches_are_not_citable() -> None:
    success = _evidence(
        "fetch-success", "web", "https://docs.python.org/3.13/whatsnew/3.13.html",
        "A supported change.",
    )
    failed = _evidence(
        "fetch-fail", "other", "http://127.0.0.1/private",
        "fetch status: blocked for http://127.0.0.1/private\ndestination is not public",
    )
    empty = _evidence(
        "fetch-empty", "other", "https://example.com/empty",
        "fetch status: empty for https://example.com/empty\nno readable text",
    )
    report = _report([
        Citation(source_type="other", source_ref=failed.source_ref, quote_or_summary="blocked"),
        Citation(source_type="other", source_ref=empty.source_ref, quote_or_summary="empty"),
    ])

    clean = sanitize_report(report, None, "r", items=[success, failed, empty])

    assert [citation.source_ref for citation in clean.citations] == [success.source_ref]
    assert clean.citations[0].source_type == "web"


def test_fallback_only_uses_citable_evidence() -> None:
    failed = _evidence(
        "fetch-fail", "other", "http://127.0.0.1/private",
        "fetch status: blocked for http://127.0.0.1/private",
    )
    empty = _evidence(
        "fetch-empty", "other", "https://example.com/empty",
        "fetch status: empty for https://example.com/empty",
    )
    success = _evidence(
        "fetch-success", "web", "https://docs.python.org/3.13/whatsnew/3.13.html",
        "A supported change.",
    )

    clean = sanitize_report(_report(), None, "r", items=[failed, empty, success])

    assert [citation.source_ref for citation in clean.citations] == [success.source_ref]
    assert clean.citations[0].source_type == "web"


def test_no_citable_evidence_means_no_fallback_citation() -> None:
    failed = _evidence(
        "fetch-fail", "other", "http://127.0.0.1/private",
        "fetch status: blocked for http://127.0.0.1/private",
    )
    empty = _evidence(
        "fetch-empty", "other", "https://example.com/empty",
        "fetch status: empty for https://example.com/empty",
    )

    clean = sanitize_report(_report(), None, "r", items=[failed, empty])

    assert clean.citations == []
    assert clean.tools_used == []


def test_fetch_page_is_attributed_only_for_successful_fetches() -> None:
    success = _evidence(
        "fetch-success", "web", "https://docs.python.org/3.13/whatsnew/3.13.html",
        "A supported change.",
    )
    failed = _evidence(
        "fetch-fail", "other", "http://127.0.0.1/private",
        "fetch status: blocked for http://127.0.0.1/private",
    )
    regular_web = _evidence(
        "web-search", "web", "https://example.com/search-result",
        "A search snippet.",
    )

    clean = sanitize_report(_report(), None, "r", items=[success, failed, regular_web])

    assert clean.tools_used == ["fetch_page", "web_search"]


def test_failed_fetch_alone_does_not_claim_fetch_page() -> None:
    failed = _evidence(
        "fetch-fail", "other", "http://127.0.0.1/private",
        "fetch status: blocked for http://127.0.0.1/private",
    )

    clean = sanitize_report(_report(), None, "r", items=[failed])

    assert clean.tools_used == []
