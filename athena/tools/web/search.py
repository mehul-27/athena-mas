"""Web search over free, keyless endpoints.

Ported from the old Athena `search/web.py`: DuckDuckGo's HTML endpoint, scraped,
with its ``//duckduckgo.com/l/?uddg=`` redirects unwrapped. SearXNG is kept as
the opt-in JSON alternative. The keyed providers (Brave, Tavily, Serper, Google
PSE) are dropped: Athena's own evaluation never used them, and each one adds a
credential path to guard.

Search results are untrusted input. This module returns them verbatim; deciding
what to do with that text is a guardrail's job, not the tool's.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from athena.tools import settings
from athena.tools.schemas import WebHit, WebResult


def _is_duckduckgo_host(host: str) -> bool:
    host = (host or "").lower()
    return host == "duckduckgo.com" or host.endswith(".duckduckgo.com")


def resolve_redirect(raw: str) -> str:
    """Unwrap DuckDuckGo's ``/l/?uddg=<real url>`` redirect."""
    if not raw:
        return raw
    resolved = raw
    if resolved.startswith("//"):
        resolved = "https:" + resolved
    elif resolved.startswith("/"):
        resolved = urljoin("https://html.duckduckgo.com", resolved)
    try:
        parsed = urlparse(resolved)
        if _is_duckduckgo_host(parsed.hostname) and parsed.path.rstrip("/") == "/l":
            query = parse_qs(parsed.query)
            if "uddg" in query:
                return query["uddg"][0]
    except ValueError:
        pass
    return resolved


_AD_URL_MARKERS = ("ad_domain=", "ad_provider=", "aclick", "/y.js")


def is_ad_url(url: str) -> bool:
    """True for advertising/tracking redirects that are not real results.

    The DuckDuckGo scrape returns sponsored blocks among the organic ones; their
    ``href`` is a click tracker (``duckduckgo.com/y.js?ad_domain=...``) that
    eventually lands on a ``bing.com/aclick`` hop. None of that is evidence.
    """
    lowered = (url or "").lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in _AD_URL_MARKERS)


def duckduckgo(query: str, count: int) -> list[WebHit]:
    """Scrape the HTML endpoint. No API key, no quota to configure."""
    response = httpx.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": settings.user_agent()},
        timeout=settings.search_timeout(),
        follow_redirects=True,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    hits: list[WebHit] = []
    for block in soup.select(".result"):
        if len(hits) >= count:
            break
        link = block.select_one(".result__a")
        if not link:
            continue
        url = resolve_redirect(link.get("href", ""))
        if not url or is_ad_url(url):
            continue
        snippet = block.select_one(".result__snippet")
        hits.append(
            WebHit(
                title=link.get_text(" ", strip=True),
                url=url,
                snippet=snippet.get_text(" ", strip=True) if snippet else "",
            )
        )
    return hits


def searxng(query: str, count: int) -> list[WebHit]:
    """Query a self-hosted SearXNG instance's JSON API."""
    instance = settings.searxng_instance()
    if not instance:
        raise ValueError("SEARCH_PROVIDER=searxng requires SEARXNG_INSTANCE")

    response = httpx.get(
        f"{instance}/search",
        params={"q": query, "format": "json", "language": "en"},
        headers={"User-Agent": settings.user_agent()},
        timeout=settings.search_timeout(),
    )
    response.raise_for_status()
    hits: list[WebHit] = []
    for item in response.json().get("results", []):
        if len(hits) >= count:
            break
        url = item.get("url", "")
        if not url or is_ad_url(url):
            continue
        hits.append(
            WebHit(
                title=item.get("title", ""),
                url=url,
                snippet=item.get("content", ""),
            )
        )
    return hits


def web_search(query: str, max_results: int = 5) -> WebResult:
    """Search the web. Never raises: failures come back as ``ok=False``."""
    started = time.perf_counter()
    provider = settings.search_provider()
    limit = max(1, min(int(max_results), 20))

    def result(**kwargs) -> WebResult:
        return WebResult(
            query=query,
            provider=provider,
            max_results=limit,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            **kwargs,
        )

    if provider == "disabled":
        return result(ok=False, error="web search is disabled (SEARCH_PROVIDER=disabled)")

    callers = {"duckduckgo": duckduckgo, "searxng": searxng}
    caller = callers.get(provider)
    if caller is None:
        return result(
            ok=False,
            error=f"unsupported SEARCH_PROVIDER {provider!r} "
            f"(use one of {sorted(callers)} or disabled)",
        )

    try:
        return result(results=caller(query, limit))
    except Exception as exc:  # noqa: BLE001 - network failure is a result, not a crash
        return result(ok=False, error=f"{type(exc).__name__}: {exc}")
