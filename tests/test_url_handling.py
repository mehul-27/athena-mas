"""Plain-URL handling and citable-evidence selection."""

from __future__ import annotations

from types import SimpleNamespace

from athena.agents import bb_io, researcher
from athena.agents.untrusted import UNTRUSTED_BEGIN
from athena.blackboard import Blackboard, EvidenceItem, SQLiteStore, Task
from athena.patterns.context import P2PContext
from athena.tools.web.urls import plain_http_url


def test_markdown_and_angle_bracket_urls_are_normalized() -> None:
    url = "https://docs.python.org/3.13/whatsnew/3.13.html"
    assert plain_http_url(f"[What's New]({url})") == url
    assert plain_http_url(f"[What's New](<{url}>)") == url
    assert plain_http_url(f"<{url}>") == url
    assert plain_http_url(url) == url
    assert plain_http_url("[not a link]") == "[not a link]"


def test_fetch_callback_receives_a_plain_url() -> None:
    seen = {}
    tool = SimpleNamespace(name="fetch_page")

    def budget(tool_arg, args, _context):
        seen["tool"] = tool_arg.name
        seen["url"] = args["url"]
        return None

    callback = researcher.plain_fetch_urls(budget)
    args = {"url": "[docs](https://docs.python.org/3.13/)", "max_chars": 1000}
    assert callback(tool, args, None) is None
    assert seen["url"] == "https://docs.python.org/3.13/"
    assert args["url"] == "https://docs.python.org/3.13/"


def _evidence(evidence_id: str, source_type: str, source_ref: str, content: str) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id, source_type=source_type, source_ref=source_ref,
        content=content, retrieved_by="researcher",
    )


def test_evidence_text_prioritizes_fetched_pages_and_drops_provenance(tmp_path) -> None:
    blackboard = Blackboard.create(Task(question="q"), store=SQLiteStore(tmp_path / "bb.db"))
    items = [
        _evidence("web-1", "web", "https://search.example/one", "search snippet"),
        _evidence("fetch-1", "web", "https://docs.example/primary", "page text"),
        _evidence("other-1", "other", "http://127.0.0.1/blocked", "fetch status: blocked"),
        _evidence("doc_1", "rag", "handbook.pdf#p1", "corpus text"),
    ]
    for item in items:
        blackboard.append("evidence", item, author="researcher")

    text = bb_io.evidence_text(blackboard)

    assert "https://docs.example/primary" in text
    assert "https://search.example/one" in text
    assert "handbook.pdf#p1" in text
    assert "127.0.0.1" not in text, "failed fetch provenance is not usable evidence"
    assert text.index("fetch-1") < text.index("doc_1") < text.index("web-1")
    assert UNTRUSTED_BEGIN in text


def test_p2p_evidence_block_uses_the_same_selection_policy() -> None:
    items = [
        _evidence("web-1", "web", "https://search.example/one", "snippet"),
        _evidence("fetch-1", "web", "https://docs.example/primary", "page"),
        _evidence("other-1", "other", "http://127.0.0.1/blocked", "blocked"),
    ]
    text = P2PContext(question="q", evidence=items).evidence_block()

    assert "https://docs.example/primary" in text
    assert "127.0.0.1" not in text
    assert text.index("fetch-1") < text.index("web-1")
