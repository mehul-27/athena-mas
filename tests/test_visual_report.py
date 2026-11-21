from athena.api.visual_report import render_visual_report


def test_visual_report_contains_research_and_printable_sources():
    html = render_visual_report(
        run_id="run-123",
        question="How does <rocket> work?",
        report={
            "answer": "A supported answer.",
            "key_points": ["First finding."],
            "confidence": 0.8,
            "citations": [{
                "source_type": "web",
                "source_ref": "https://example.com/source",
                "quote_or_summary": "Primary source excerpt.",
            }],
            "guardrail_flags": [],
        },
        critique={"approved": True},
        summary={"outcome": "approved"},
        evidence=[],
    )

    assert "How does &lt;rocket&gt; work?" in html
    assert "A supported answer." in html
    assert "First finding." in html
    assert 'href="https://example.com/source"' in html
    assert "Save as PDF" in html
    assert "window.print()" in html
    assert "@media print" in html


def test_visual_report_never_links_non_http_source_refs():
    html = render_visual_report(
        run_id="run-123",
        question="Question",
        report={"answer": "Answer", "citations": [{
            "source_type": "web", "source_ref": "javascript:alert(1)",
            "quote_or_summary": "Text",
        }]},
        critique=None,
        summary={},
        evidence=[],
    )

    assert "href=\"javascript:" not in html
    assert "javascript:alert(1)" in html
