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


def test_claims_absent_from_a_truncated_stub_are_removed() -> None:
    stub = _evidence(
        "fetch-314", "web", "https://docs.python.org/3/whatsnew/3.14.html",
        "What's new in Python 3.14. Python 3.14 ...",
    )
    report = FinalReport(
        answer="Python 3.14 added template strings and compression.zstd.",
        key_points=["Python 3.14 introduced deferred annotations."],
        citations=[
            Citation(
                source_type="web", source_ref=stub.source_ref,
                quote_or_summary="Python 3.14 ...",
            )
        ],
        confidence=0.8,
        run_id="r",
    )

    clean = sanitize_report(report, None, "r", items=[stub], question="What changed in Python 3.14?")

    assert "template" not in clean.answer.lower()
    assert "zstd" not in clean.answer.lower()
    assert clean.confidence <= 0.3
    assert not clean.citations


def test_each_surviving_claim_keeps_a_supporting_citation() -> None:
    first = _evidence(
        "fetch-alpha", "web", "https://example.com/alpha",
        "Alpha thrusters use pressurized nitrogen and a nozzle. " * 3,
    )
    second = _evidence(
        "fetch-beta", "web", "https://example.com/beta",
        "Beta thrusters use hydrazine and a catalyst bed. " * 3,
    )
    report = FinalReport(
        answer="Alpha thrusters use pressurized nitrogen and a nozzle. Beta thrusters use hydrazine and a catalyst bed.",
        key_points=[],
        citations=[
            Citation(
                source_type="web", source_ref=first.source_ref,
                quote_or_summary="Alpha.",
            )
        ],
        confidence=0.7,
        run_id="r",
    )

    clean = sanitize_report(
        report, None, "r", items=[first, second],
        question="Compare alpha and beta thrusters.",
    )

    refs = {citation.source_ref for citation in clean.citations}
    assert first.source_ref in refs
    assert second.source_ref in refs
    assert "Beta thrusters" in clean.answer


def test_a_claim_is_kept_when_the_cited_content_supports_it() -> None:
    evidence = _evidence(
        "fetch-315", "web", "https://docs.python.org/3/whatsnew/3.15.html",
        "Python 3.15 adds lazy imports and a frozendict builtin. "
        "The release also makes UTF-8 the default encoding. " * 2,
    )
    report = FinalReport(
        answer="Python 3.15 adds lazy imports and a frozendict builtin.",
        key_points=["The release makes UTF-8 the default encoding."],
        citations=[
            Citation(
                source_type="web", source_ref=evidence.source_ref,
                quote_or_summary="Python 3.15 ...",
            )
        ],
        confidence=0.7,
        run_id="r",
    )

    clean = sanitize_report(
        report, None, "r", items=[evidence], question="What changed in Python 3.15?"
    )

    assert "lazy imports" in clean.answer
    assert clean.citations
    assert clean.confidence == 0.7


def test_named_entity_evidence_is_backfilled_into_citations() -> None:
    generic = _evidence(
        "web-generic", "web", "https://example.com/cold-gas",
        "A cold gas thruster expands pressurized gas through a nozzle.",
    )
    named_fetch = _evidence(
        "fetch-spacex", "web", "https://example.com/spacex-cold-gas",
        "SpaceX cold-gas thruster hardware description.",
    )
    named_snippet = _evidence(
        "web-spacex", "web", "https://en.wikipedia.org/wiki/SpaceX_rocket_engines",
        "SpaceX rocket engines include Merlin, Draco and SuperDraco.",
    )
    unread_pdf = _evidence(
        "web-pdf", "web", "https://example.com/spacex-paper.pdf",
        "SpaceX paper snippet.",
    )
    report = _report([
        Citation(source_type="web", source_ref=generic.source_ref, quote_or_summary="definition")
    ])

    clean = sanitize_report(
        report, None, "r", items=[generic, named_fetch, named_snippet, unread_pdf],
        question="How does SpaceX's nitrogen thruster work in space?",
    )

    refs = [citation.source_ref for citation in clean.citations]
    assert generic.source_ref in refs
    assert named_fetch.source_ref in refs
    assert named_snippet.source_ref in refs
    assert unread_pdf.source_ref not in refs


def test_pdf_search_snippet_is_not_citable_as_a_read_source() -> None:
    pdf_snippet = _evidence(
        "web-pdf", "web", "https://example.com/paper.pdf",
        "PDF A Detailed Study of Cold Gas Propulsion",
    )
    html_snippet = _evidence(
        "web-html", "web", "https://example.com/article",
        "Cold gas thruster definition",
    )
    report = _report([
        Citation(source_type="web", source_ref=pdf_snippet.source_ref, quote_or_summary="snippet"),
        Citation(source_type="web", source_ref=html_snippet.source_ref, quote_or_summary="snippet"),
    ])

    clean = sanitize_report(report, None, "r", items=[pdf_snippet, html_snippet])

    assert [citation.source_ref for citation in clean.citations] == [html_snippet.source_ref]


def test_failed_fetch_alone_does_not_claim_fetch_page() -> None:
    failed = _evidence(
        "fetch-fail", "other", "http://127.0.0.1/private",
        "fetch status: blocked for http://127.0.0.1/private",
    )

    clean = sanitize_report(_report(), None, "r", items=[failed])

    assert clean.tools_used == []
