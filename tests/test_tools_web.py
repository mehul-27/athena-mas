"""Offline tests for web_search: the HTTP layer is mocked, no network is used."""

from __future__ import annotations

import httpx
import pytest

from athena.tools.web.search import is_ad_url, web_search

DDG_HTML_WITH_AD = """
<html><body>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/y.js?ad_domain=abebooks.com&ad_provider=bingv7aa&u3=https%3A%2F%2Fwww.bing.com%2Faclick%3Fld%3Dx">
      Sponsored: NASA Handbook
    </a>
    <a class="result__snippet">Buy it now.</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://www.nasa.gov/reference/systems-engineering-handbook/">Real NASA page</a>
    <a class="result__snippet">The handbook.</a>
  </div>
</body></html>
"""

DDG_HTML = """
<html><body>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.nasa.gov%2Fse%2F">
      NASA Systems Engineering
    </a>
    <a class="result__snippet">The NASA systems engineering handbook.</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://example.com/other">Other result</a>
    <a class="result__snippet">Something else entirely.</a>
  </div>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def test_web_search_parses_a_canned_response(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _FakeResponse(DDG_HTML))

    result = web_search("nasa systems engineering", max_results=5)

    assert result.ok, result.error
    assert result.tool_name == "web_search"
    assert result.provider == "duckduckgo"
    assert [hit.title for hit in result.results] == [
        "NASA Systems Engineering",
        "Other result",
    ]
    # The provider's redirect wrapper must be unwrapped to the real target.
    assert result.results[0].url == "https://www.nasa.gov/se/"
    assert result.results[0].snippet == "The NASA systems engineering handbook."
    assert result.results[1].url == "https://example.com/other"


def test_web_search_reports_network_failure(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", "duckduckgo")

    def boom(*args, **kwargs):
        raise httpx.ConnectError("no network here")

    monkeypatch.setattr(httpx, "get", boom)

    result = web_search("nasa", max_results=3)

    assert not result.ok
    assert "no network here" in (result.error or "")
    assert result.results == []


def test_web_search_reports_http_error_status(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", "duckduckgo")

    class _Failing(_FakeResponse):
        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError("403", request=None, response=None)

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _Failing(""))

    result = web_search("nasa", max_results=3)
    assert not result.ok
    assert "HTTPStatusError" in (result.error or "")


def test_web_search_reports_disabled_provider(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", "disabled")
    result = web_search("nasa", max_results=3)
    assert not result.ok
    assert "disabled" in (result.error or "").lower()


def test_web_search_reports_unknown_provider(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", "brave")
    result = web_search("nasa", max_results=3)
    assert not result.ok
    assert "unsupported" in (result.error or "").lower()

def test_web_search_drops_ad_and_tracking_redirects(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _FakeResponse(DDG_HTML_WITH_AD))

    result = web_search("nasa handbook", max_results=5)

    assert result.ok, result.error
    assert [hit.url for hit in result.results] == [
        "https://www.nasa.gov/reference/systems-engineering-handbook/"
    ]
    assert all("ad_domain" not in hit.url for hit in result.results)


def test_is_ad_url_recognises_trackers() -> None:
    assert is_ad_url("https://duckduckgo.com/y.js?ad_domain=x")
    assert is_ad_url("https://www.bing.com/aclick?ld=1")
    assert is_ad_url("https://example.com/click?ad_provider=bing")
    assert not is_ad_url("https://www.nasa.gov/reference/systems-engineering-handbook/")
    assert not is_ad_url("")
