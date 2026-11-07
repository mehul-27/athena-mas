"""Offline SearXNG adapter tests; the live endpoint test skips when it is down."""
from __future__ import annotations

import json
import socket

import httpx
import pytest

from athena.tools.web import search

LIVE_URL = "http://127.0.0.1:8080"


class FakeResponse:
    def __init__(self, status=200, payload=None, text="", json_error=False):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = text.encode()
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://127.0.0.1:8080/search"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        if self._json_error:
            raise ValueError("malformed JSON")
        return self._payload if self._payload is not None else {"results": []}


@pytest.fixture(autouse=True)
def reset_health():
    search.reset_search_health()
    yield
    search.reset_search_health()


def test_searxng_uses_base_url_appends_search_and_parses_fields(monkeypatch):
    captured = {}

    def fake_fetch(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return FakeResponse(payload={"results": [
            {
                "title": "Hall thruster",
                "url": "https://example.gov/hall",
                "content": "A Hall thruster accelerates ions.",
            },
            {
                "title": "Second",
                "url": "https://example.gov/second",
                "content": "Another result.",
            },
        ]})

    monkeypatch.setattr(search, "_fetch", fake_fetch)
    result = search.web_search(
        "Hall thruster", 5, provider="searxng", endpoint="http://127.0.0.1:8080"
    )

    assert result.ok is True
    assert result.provider == "searxng"
    assert captured["url"] == "http://127.0.0.1:8080/search"
    assert captured["params"]["q"] == "Hall thruster"
    assert captured["params"]["format"] == "json"
    assert [hit.title for hit in result.results] == ["Hall thruster", "Second"]
    assert result.results[0].url == "https://example.gov/hall"
    assert result.results[0].snippet == "A Hall thruster accelerates ions."


def test_searxng_empty_results_are_an_empty_search(monkeypatch):
    monkeypatch.setattr(search, "_fetch", lambda *a, **k: FakeResponse(payload={"results": []}))

    result = search.web_search(
        "nothing", 3, provider="searxng", endpoint="http://127.0.0.1:8080"
    )

    assert result.ok is True
    assert result.results == []
    assert "no results" in (result.note or "").lower()


def test_searxng_malformed_json_is_a_failure(monkeypatch):
    monkeypatch.setattr(
        search, "_fetch",
        lambda *a, **k: FakeResponse(payload=None, json_error=True),
    )

    result = search.web_search(
        "hall", 3, provider="searxng", endpoint="http://127.0.0.1:8080"
    )

    assert result.ok is False
    assert "malformed JSON" in (result.error or "")


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_searxng_http_errors_are_bounded(monkeypatch, status):
    monkeypatch.setattr(search, "_sleep", lambda seconds: None)
    monkeypatch.setattr(
        search, "_fetch",
        lambda *a, **k: FakeResponse(status=status, text="provider failure"),
    )

    result = search.web_search(
        "hall", 3, provider="searxng", endpoint="http://127.0.0.1:8080"
    )

    assert result.ok is False
    assert "500" in (result.error or "") or "503" in (result.error or "") or "throttled" in (result.error or "")


def test_searxng_timeout_and_connection_reset_are_classified(monkeypatch):
    for error in (httpx.ConnectTimeout("timeout"), httpx.ConnectError("reset")):
        search.reset_search_health()
        monkeypatch.setattr(search, "_fetch", lambda *a, **k: (_ for _ in ()).throw(error))
        result = search.web_search(
            "hall", 3, provider="searxng", endpoint="http://127.0.0.1:8080"
        )
        assert result.ok is False
        assert type(error).__name__ in (result.error or "")


def test_duckduckgo_throttle_uses_searxng_and_preserves_attribution(monkeypatch):
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "searxng")
    monkeypatch.setenv("SEARCH_FALLBACK_ENDPOINT", "http://127.0.0.1:8080")
    monkeypatch.setattr(
        search, "duckduckgo",
        lambda query, count: (_ for _ in ()).throw(search.SearchThrottled()),
    )
    calls = []

    def fallback(query, count, endpoint=None):
        calls.append((query, endpoint))
        return [search.WebHit(title="Fallback", url="https://example.gov/f", snippet="ok")]

    monkeypatch.setattr(search, "searxng", fallback)
    result = search.web_search("hall", 3)

    assert result.ok is True
    assert result.provider == "searxng"
    assert calls == [("hall", "http://127.0.0.1:8080")]
    assert "fallback" in (result.note or "").lower()


def test_both_providers_failing_does_not_loop(monkeypatch):
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "searxng")
    monkeypatch.setenv("SEARCH_FALLBACK_ENDPOINT", "http://127.0.0.1:8080")
    calls = []

    def fail(query, count, endpoint=None):
        calls.append(endpoint)
        raise search.SearchThrottled()

    monkeypatch.setattr(search, "duckduckgo", lambda query, count: fail(query, count))
    monkeypatch.setattr(search, "searxng", fail)
    result = search.web_search("hall", 3)

    assert result.ok is False
    assert len(calls) == 2
    assert "search failed" in (result.error or "")
    assert "fallback failed" in (result.error or "")


# --------------------------------------------------------------- URL handling


def test_a_base_url_that_already_ends_with_search_is_not_doubled(monkeypatch):
    captured = {}

    def fake_fetch(url, **kwargs):
        captured["url"] = url
        return FakeResponse(payload={"results": []})

    monkeypatch.setattr(search, "_fetch", fake_fetch)
    search.web_search(
        "hall", 3, provider="searxng", endpoint="http://127.0.0.1:8080/search/"
    )

    assert captured["url"] == "http://127.0.0.1:8080/search"


# ------------------------------------------------------- attribution/identity


def test_every_hit_records_the_provider_that_produced_it(monkeypatch):
    monkeypatch.setattr(
        search, "_fetch",
        lambda *a, **k: FakeResponse(payload={"results": [
            {"title": "A", "url": "https://example.gov/a", "content": "a"},
            {"title": "B", "url": "https://example.gov/b", "content": "b"},
        ]}),
    )

    result = search.web_search("hall", 5, provider="searxng", endpoint=LIVE_URL)

    assert result.provider == "searxng"
    assert [hit.provider for hit in result.results] == ["searxng", "searxng"]


def test_duckduckgo_hits_are_attributed_to_duckduckgo(monkeypatch):
    html = (
        '<div class="result"><a class="result__a" href="https://example.gov/a">A</a>'
        '<div class="result__snippet">snippet</div></div>'
    )

    monkeypatch.setattr(
        search, "_fetch",
        lambda *a, **k: type("R", (), {
            "status_code": 200, "text": html, "raise_for_status": lambda self: None,
        })(),
    )
    hits = search.duckduckgo("hall", 3)

    assert [hit.provider for hit in hits] == ["duckduckgo"]


def test_repeated_urls_are_deduped_keeping_the_first(monkeypatch):
    payload = {"results": [
        {"title": "First", "url": "https://example.gov/page", "content": "one"},
        {"title": "Same page, trailing slash", "url": "https://example.gov/page/",
         "content": "two"},
        {"title": "Same page, uppercase host", "url": "https://EXAMPLE.gov/page#top",
         "content": "three"},
        {"title": "Different page", "url": "https://example.gov/other", "content": "x"},
        {"title": "No url at all", "url": "", "content": "y"},
    ]}
    monkeypatch.setattr(search, "_fetch", lambda *a, **k: FakeResponse(payload=payload))

    result = search.web_search("hall", 10, provider="searxng", endpoint=LIVE_URL)

    assert [hit.title for hit in result.results] == ["First", "Different page"]
    assert all(hit.url for hit in result.results)


def test_dedupe_url_ignores_scheme_case_fragment_and_default_port():
    keys = {
        search.dedupe_url("https://example.gov/a"),
        search.dedupe_url("http://example.gov/a/"),
        search.dedupe_url("https://EXAMPLE.gov:443/a#frag"),
    }

    assert len(keys) == 1
    assert search.dedupe_url("https://example.gov/a?x=1") != keys.pop()
    assert search.dedupe_url("") == ""


# --------------------------------------------------- malformed / blocked shapes


@pytest.mark.parametrize(
    "payload",
    [
        {},                                   # no results key at all
        {"results": "not-a-list"},            # wrong type
        [],                                   # not an object
        {"error": "search is not allowed"},   # provider-reported refusal
    ],
)
def test_unusable_payloads_are_failures_not_empty_results(monkeypatch, payload):
    monkeypatch.setattr(search, "_fetch", lambda *a, **k: FakeResponse(payload=payload))
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "")

    result = search.web_search("hall", 3, provider="searxng", endpoint=LIVE_URL)

    assert result.ok is False, "an unusable payload must never look like 0 results"
    assert result.results == []
    assert "expected" in (result.error or "") or "no results list" in (result.error or "")


def test_a_200_html_page_is_malformed_not_empty(monkeypatch):
    """A JSON-less instance answers 200 with HTML; that is not an empty search."""
    monkeypatch.setattr(
        search, "_fetch",
        lambda *a, **k: FakeResponse(
            status=200, text="<html><body>JSON API disabled</body></html>",
            json_error=True,
        ),
    )

    result = search.web_search("hall", 3, provider="searxng", endpoint=LIVE_URL)

    assert result.ok is False
    assert "malformed JSON" in (result.error or "")
    assert result.results == []


def test_a_blocked_searxng_is_classified(monkeypatch):
    monkeypatch.setattr(search, "_sleep", lambda seconds: None)
    monkeypatch.setattr(
        search, "_fetch",
        lambda *a, **k: FakeResponse(status=403, text="Forbidden: crawlers are blocked"),
    )

    result = search.web_search("hall", 3, provider="searxng", endpoint=LIVE_URL)

    assert result.ok is False
    assert search.last_error_class("searxng") == "throttled"


def test_result_text_containing_throttle_words_is_not_mistaken_for_throttling(
    monkeypatch,
) -> None:
    """A JSON body quotes page text; "blocked"/"captcha" there is not a refusal."""
    payload = {"results": [{
        "title": "Rate limiting",
        "url": "https://example.gov/rate-limits",
        "content": "A blocked request is rate limited; the captcha gate then opens.",
    }]}
    calls: list[str] = []

    def fake_fetch(url, **kwargs):
        calls.append(url)
        return FakeResponse(payload=payload)

    monkeypatch.setattr(search, "_fetch", fake_fetch)
    result = search.web_search("rate limit", 3, provider="searxng", endpoint=LIVE_URL)

    assert result.ok is True, result.error
    assert len(result.results) == 1
    assert calls == [f"{LIVE_URL}/search"], "a valid JSON answer must not be retried"


def test_a_captcha_page_served_as_200_is_throttling(monkeypatch):
    monkeypatch.setattr(search, "_sleep", lambda seconds: None)
    monkeypatch.setattr(
        search, "_fetch",
        lambda *a, **k: FakeResponse(
            status=200, text="<html>Our systems detected unusual traffic: captcha</html>",
            json_error=True,
        ),
    )

    result = search.web_search("hall", 3, provider="searxng", endpoint=LIVE_URL)

    assert result.ok is False
    assert search.last_error_class("searxng") == "throttled"
    assert "throttled" in (result.error or "")



# ------------------------------------------------ optional fallback (env only)


def test_athena_searxng_url_alone_enables_the_fallback(monkeypatch):
    """The documented opt-in: setting the URL is enough, no extra switch."""
    monkeypatch.delenv("SEARCH_FALLBACK_PROVIDER", raising=False)
    monkeypatch.delenv("SEARCH_FALLBACK_ENDPOINT", raising=False)
    monkeypatch.delenv("SEARXNG_INSTANCE", raising=False)
    monkeypatch.setenv("ATHENA_SEARXNG_URL", LIVE_URL)
    monkeypatch.setattr(
        search, "duckduckgo",
        lambda query, count: (_ for _ in ()).throw(search.SearchThrottled()),
    )
    calls = []

    def fallback(query, count, endpoint=None):
        calls.append(endpoint)
        return [search.WebHit(title="F", url="https://example.gov/f", snippet="s")]

    monkeypatch.setattr(search, "searxng", fallback)
    result = search.web_search("hall", 3)

    assert result.ok is True
    assert result.provider == "searxng"
    assert result.results[0].provider == "searxng"
    assert calls == [LIVE_URL]


def test_explicit_none_disables_the_implicit_fallback(monkeypatch):
    monkeypatch.setenv("ATHENA_SEARXNG_URL", LIVE_URL)
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "none")
    monkeypatch.setattr(
        search, "duckduckgo",
        lambda query, count: (_ for _ in ()).throw(search.SearchThrottled()),
    )
    called: list[str] = []
    monkeypatch.setattr(
        search, "searxng",
        lambda query, count, endpoint=None: called.append(endpoint) or [],
    )

    result = search.web_search("hall", 3)

    assert result.ok is False
    assert called == []


def test_no_fallback_is_configured_by_default(monkeypatch):
    for name in ("ATHENA_SEARXNG_URL", "SEARXNG_INSTANCE",
                 "SEARCH_FALLBACK_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "")
    monkeypatch.setattr(
        search, "duckduckgo",
        lambda query, count: (_ for _ in ()).throw(search.SearchThrottled()),
    )

    result = search.web_search("hall", 3)

    assert result.ok is False
    assert "no fallback configured" in (result.error or "")
    assert "ATHENA_SEARXNG_URL" in (result.error or "")


# --------------------------------------------------------------- live endpoint


def _live_searxng_reachable(timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8080), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.mark.skipif(
    not _live_searxng_reachable(), reason="no SearXNG listening on 127.0.0.1:8080"
)
def test_live_searxng_call(monkeypatch):
    """One real call against the local instance; skipped when it is not running."""
    monkeypatch.setenv("SEARCH_TIMEOUT", "10")
    result = search.web_search("hall effect thruster", 3, provider="searxng",
                               endpoint=LIVE_URL)

    assert result.ok is True, result.error
    assert result.provider == "searxng"
    assert result.results, "a live instance answering with no hits is worth knowing"
    for hit in result.results:
        assert hit.url.startswith("http")
        assert hit.provider == "searxng"
