"""Search provider throttling: retry twice, then fail loudly."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.tools.schemas import WebHit
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


@pytest.fixture(autouse=True)
def reset_search_circuit():
    search.reset_search_health()
    yield
    search.reset_search_health()


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
    assert "search provider throttled" in result.error
    assert "no fallback configured" in result.error
    assert result.results == []
    assert canned.sleeps == [0.5, 1.5]


def test_429_and_403_are_throttled(canned) -> None:
    for status in (429, 403):
        search.reset_search_health()
        canned.responses.extend([FakeResponse(status)] * 3)
        result = search.web_search("nasa", 3)
        assert result.ok is False
        assert "search provider throttled" in result.error


def test_a_captcha_page_is_throttled_even_with_200(canned) -> None:
    page = "<html><body>Our systems have detected unusual traffic. Please solve the captcha.</body></html>"
    canned.responses.extend([FakeResponse(200, page)] * 3)

    result = search.web_search("nasa", 3)

    assert result.ok is False
    assert "search provider throttled" in result.error


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


def test_circuit_breaker_skips_retries_against_an_unhealthy_provider(canned) -> None:
    canned.responses.extend([FakeResponse(202)] * 3)

    first = search.web_search("nasa", 3)
    calls_after_first = len(canned.calls)
    second = search.web_search("nasa", 3)

    assert first.ok is False
    assert second.ok is False
    assert "cooling down" in (second.error or "")
    assert "no fallback configured" in (second.error or "")
    assert len(canned.calls) == calls_after_first


def test_configured_fallback_provider_is_used(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "searxng")
    monkeypatch.setenv("SEARCH_FALLBACK_ENDPOINT", "http://127.0.0.1:8888")
    monkeypatch.setattr(
        search, "duckduckgo",
        lambda query, count: (_ for _ in ()).throw(search.SearchThrottled()),
    )
    monkeypatch.setattr(
        search, "searxng",
        lambda query, count, endpoint=None: [
            WebHit(title="Fallback", url="https://example.gov/fallback", snippet="ok")
        ],
    )

    result = search.web_search("nasa", 3)

    assert result.ok is True
    assert result.provider == "searxng"
    assert result.results[0].url == "https://example.gov/fallback"
    assert "fallback" in (result.note or "").lower()


# ------------------------------------------------------------- bounded retries


def test_a_throttled_provider_makes_exactly_the_bounded_number_of_attempts(canned) -> None:
    canned.responses.extend([FakeResponse(202)] * 10)

    result = search.web_search("nasa", 3)

    assert result.ok is False
    assert len(canned.calls) == search.MAX_ATTEMPTS
    assert search.MAX_ATTEMPTS == len(search.RETRY_DELAYS) + 1
    assert len(canned.sleeps) == search.MAX_ATTEMPTS - 1


def test_the_fallback_has_its_own_bounded_budget(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "searxng")
    monkeypatch.setenv("SEARCH_FALLBACK_ENDPOINT", "http://127.0.0.1:8080")
    attempts = {"duckduckgo": 0, "searxng": 0}

    def counting(name):
        def call(query, count, endpoint=None):
            attempts[name] += 1
            raise search.SearchThrottled()
        return call

    monkeypatch.setattr(
        search, "duckduckgo",
        lambda query, count: counting("duckduckgo")(query, count),
    )
    monkeypatch.setattr(search, "searxng", counting("searxng"))

    result = search.web_search("nasa", 3)

    # The adapter functions are the retry boundary, so a throttled primary plus a
    # throttled fallback is exactly one call each here: no loop, no second round.
    assert result.ok is False
    assert attempts == {"duckduckgo": 1, "searxng": 1}
    assert "fallback failed" in (result.error or "")


def test_the_cooldown_skips_the_primary_and_still_reaches_the_fallback(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "searxng")
    monkeypatch.setenv("SEARCH_FALLBACK_ENDPOINT", "http://127.0.0.1:8080")
    primary_calls: list[str] = []
    fallback_calls: list[str] = []

    def primary(query, count):
        primary_calls.append(query)
        raise search.SearchThrottled()

    def fallback(query, count, endpoint=None):
        fallback_calls.append(query)
        return [WebHit(title="F", url="https://example.gov/f", snippet="s")]

    monkeypatch.setattr(search, "duckduckgo", primary)
    monkeypatch.setattr(search, "searxng", fallback)

    first = search.web_search("nasa", 3)
    second = search.web_search("nasa", 3)

    assert first.ok is True and second.ok is True
    assert len(primary_calls) == 1, "the cooled-down primary must not be called again"
    assert len(fallback_calls) == 2
    assert "cooling down" in (second.note or "") or second.provider == "searxng"


def test_an_empty_primary_result_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "searxng")
    monkeypatch.setenv("SEARCH_FALLBACK_ENDPOINT", "http://127.0.0.1:8080")
    monkeypatch.setattr(search, "duckduckgo", lambda query, count: [])
    monkeypatch.setattr(
        search, "searxng",
        lambda query, count, endpoint=None: [
            WebHit(title="F", url="https://example.gov/f", snippet="s")
        ],
    )

    result = search.web_search("nasa", 3)

    assert result.ok is True
    assert result.provider == "searxng"
    assert [hit.provider for hit in result.results] == ["searxng"]


def test_a_throttled_search_never_fabricates_hits(canned) -> None:
    canned.responses.extend([FakeResponse(202)] * 3)

    result = search.web_search("nasa", 3)

    assert result.ok is False
    assert result.results == []


def test_an_empty_query_is_refused_before_any_provider_call(canned) -> None:
    for query in ("", "   "):
        result = search.web_search(query, 3)
        assert result.ok is False
        assert "query must not be empty" in (result.error or "")

    assert canned.calls == []


def test_error_classes_are_normalized_and_recorded() -> None:
    import httpx

    assert search.classify_error(search.SearchThrottled()) == "throttled"
    assert search.classify_error(search.SearchMalformed("x")) == "malformed_response"
    assert search.classify_error(httpx.ConnectTimeout("t")) == "timeout"
    assert search.classify_error(httpx.ConnectError("c")) == "network_error"
    assert search.classify_error(httpx.HTTPStatusError(
        "500", request=httpx.Request("GET", "https://x"), response=httpx.Response(500)
    )) == "http_error"
    assert search.classify_error(None) == ""

