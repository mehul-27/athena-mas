"""Search provider throttling: retry twice, then fail loudly."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.tools.web import search


class FakeResponse:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode()

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return {"results": []}


@pytest.fixture()
def canned(monkeypatch):
    """Inject the HTTP layer and record sleeps instead of waiting."""
    sleeps: list[float] = []
    responses: list[FakeResponse] = []
    calls: list[str] = []

    def fake_fetch(url, **_kwargs):
        calls.append(url)
        return responses.pop(0) if responses else FakeResponse(200, "")

    monkeypatch.setattr(search, "_fetch", fake_fetch)
    monkeypatch.setattr(search, "_sleep", lambda seconds: sleeps.append(seconds))
    return SimpleNamespace(responses=responses, sleeps=sleeps, calls=calls)


def test_202_is_retried_twice_then_throttled(canned) -> None:
    canned.responses.extend([FakeResponse(202), FakeResponse(202), FakeResponse(202)])

    result = search.web_search("nasa", 3)

    assert result.ok is False
    assert result.error == "search provider throttled"
    assert result.results == []
    assert canned.sleeps == [0.5, 1.5]


def test_429_and_403_are_throttled(canned) -> None:
    for status in (429, 403):
        canned.responses.extend([FakeResponse(status)] * 3)
        result = search.web_search("nasa", 3)
        assert result.ok is False
        assert result.error == "search provider throttled"


def test_a_captcha_page_is_throttled_even_with_200(canned) -> None:
    page = "<html><body>Our systems have detected unusual traffic. Please solve the captcha.</body></html>"
    canned.responses.extend([FakeResponse(200, page)] * 3)

    result = search.web_search("nasa", 3)

    assert result.ok is False
    assert result.error == "search provider throttled"


def test_a_retry_that_succeeds_returns_results(canned) -> None:
    html = (
        '<div class="result"><a class="result__a" href="https://example.gov/a">A</a>'
        '<div class="result__snippet">snippet</div></div>'
    )
    canned.responses.extend([FakeResponse(429), FakeResponse(200, html)])

    result = search.web_search("nasa", 3)

    assert result.ok is True, result.error
    assert len(result.results) == 1
    assert canned.sleeps == [0.5]


def test_an_empty_200_is_a_success_with_a_note(canned) -> None:
    canned.responses.append(FakeResponse(200, "<html><body>no results</body></html>"))

    result = search.web_search("nasa", 3)

    assert result.ok is True
    assert result.results == []
    assert "no results" in (result.note or "")


def test_a_throttled_search_records_no_evidence(monkeypatch, tmp_path) -> None:
    from athena.agents.evidence_recorder import record_tool_result
    from athena.blackboard import Blackboard, SQLiteStore, Task

    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=store)

    throttled = {
        "tool_name": "web_search", "elapsed_ms": 1.0, "ok": False,
        "error": "search provider throttled", "query": "nasa",
        "provider": "duckduckgo", "max_results": 3, "results": [],
    }
    written = record_tool_result("run", "web_search",
                                 {"structuredContent": throttled},
                                 store, author="web_researcher")

    assert written == 0
    assert blackboard.read("evidence") is None


def test_looks_throttled_markers() -> None:
    assert search.looks_throttled(200, "please solve the CAPTCHA")
    assert search.looks_throttled(429, "")
    assert not search.looks_throttled(200, "<div class='result'>fine</div>")
