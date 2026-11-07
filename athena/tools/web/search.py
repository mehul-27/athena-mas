"""Web search over free, keyless endpoints.

Ported from the old Athena `search/web.py`: DuckDuckGo's HTML endpoint, scraped,
with its ``//duckduckgo.com/l/?uddg=`` redirects unwrapped. SearXNG (a local,
self-hosted JSON API) is an *optional* fallback: DuckDuckGo stays primary, and
SearXNG is only consulted when the primary is throttled, unavailable or returns
nothing. Setting ``ATHENA_SEARXNG_URL`` is the whole opt-in; ``SEARCH_PROVIDER``
may also select SearXNG directly. The keyed providers (Brave, Tavily, Serper,
Google PSE) are dropped: Athena's own evaluation never used them, and each one
adds a credential path to guard.

Three invariants hold for every result this module returns:

- every hit carries the provider that produced it, so a fallback hit can never
  be attributed to the primary provider;
- results are deduped by URL, so one page is one source;
- a provider that fails, throttles or answers with garbage produces ``ok=False``
  and zero results. Nothing is ever synthesized to fill the gap, and a snippet is
  never upgraded to page text (that is ``fetch_page``'s job).

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


PROVIDERS = ("duckduckgo", "searxng")

THROTTLE_STATUS = (202, 403, 429)
RETRY_DELAYS = (0.5, 1.5)
# One first try plus one per configured delay: the retry loop is bounded, and a
# bounded loop is the only thing standing between a throttling provider and an
# infinite one.
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
THROTTLE_MARKERS = (
    "captcha", "anomaly", "unusual traffic", "are you a robot",
    "too many requests", "blocked", "rate limit",
)


class SearchThrottled(RuntimeError):
    """The provider refused to answer; retrying did not help."""


class SearchMalformed(RuntimeError):
    """The provider answered with something that is not a usable result set."""


# Provider -> normalized error class of the most recent failure. Diagnostics read
# this; it holds a class name, never a query, a URL with credentials, or a key.
_LAST_ERROR: dict[str, str] = {}

_UNHEALTHY_UNTIL: dict[str, float] = {}


def _health_key(provider: str, query: str) -> str:
    # A provider can throttle one query while serving another. The cooldown is
    # therefore scoped to the exact query; a fresh neutral query is not blocked
    # merely because a brand-heavy variant was rejected.
    return f"{provider}\x00{' '.join((query or '').lower().split())}"


def reset_search_health(provider: str | None = None) -> None:
    """Test hook and explicit recovery reset for query/provider cooldowns."""
    if provider is None:
        _UNHEALTHY_UNTIL.clear()
        _LAST_ERROR.clear()
        return
    for key in list(_UNHEALTHY_UNTIL):
        if key.startswith(provider + "\x00"):
            _UNHEALTHY_UNTIL.pop(key, None)
    _LAST_ERROR.pop(provider, None)


def last_error_class(provider: str) -> str:
    """The normalized class of the provider's most recent failure ("" if none)."""
    return _LAST_ERROR.get(provider, "")


def classify_error(exc: BaseException | None) -> str:
    """Collapse an exception into one stable, loggable error class.

    Diagnostics and telemetry must never carry a query or a response body, so
    only this vocabulary leaves the adapter.
    """
    if exc is None:
        return ""
    if isinstance(exc, SearchThrottled):
        return "throttled"
    if isinstance(exc, SearchMalformed):
        return "malformed_response"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_error"
    if isinstance(exc, httpx.TransportError):
        return "network_error"
    if isinstance(exc, ValueError):
        return "invalid_configuration"
    return "error"


def _is_healthy(provider: str, query: str) -> bool:
    key = _health_key(provider, query)
    until = _UNHEALTHY_UNTIL.get(key)
    if until is None:
        return True
    if time.monotonic() >= until:
        _UNHEALTHY_UNTIL.pop(key, None)
        return True
    return False


def _mark_unhealthy(provider: str, query: str) -> None:
    _UNHEALTHY_UNTIL[_health_key(provider, query)] = time.monotonic() + max(
        1.0, settings.search_cooldown()
    )


def _fetch(url: str, **kwargs):
    """The HTTP call, isolated so tests can inject canned responses."""
    return httpx.get(url, **kwargs)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def looks_throttled(status: int, text: str) -> bool:
    if status in THROTTLE_STATUS:
        return True
    lowered = (text or "").lower()
    return any(marker in lowered for marker in THROTTLE_MARKERS)


def dedupe_url(url: str) -> str:
    """A comparison key for a result URL.

    Two providers happily return the same page as ``https://Example.com/a``,
    ``http://example.com/a/`` and ``https://example.com/a#frag``. They are one
    source, so the dedupe key ignores scheme, case, a default port, a trailing
    slash and the fragment. The query string is kept: a different query is a
    different page.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlparse(raw)
    except ValueError:
        return raw
    if not parts.scheme or not parts.netloc:
        return raw
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return raw
    try:
        port = parts.port
    except ValueError:
        port = None
    if port in (80, 443) or port is None:
        netloc = host
    else:
        netloc = f"{host}:{port}"
    path = parts.path.rstrip("/") or "/"
    return f"//{netloc}{path}" + (f"?{parts.query}" if parts.query else "")


def dedupe_hits(hits: list[WebHit]) -> list[WebHit]:
    """Drop repeated URLs, keeping the first (highest-ranked) occurrence.

    A hit with no usable URL is not a source and is dropped rather than passed
    on as one.
    """
    seen: set[str] = set()
    unique: list[WebHit] = []
    for hit in hits:
        key = dedupe_url(hit.url)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


def fetch_with_retry(url: str, *, throttle_markers: bool = True, **kwargs):
    """Retry a throttled search at most ``MAX_ATTEMPTS`` times, then give up.

    ``throttle_markers=False`` limits the decision to the HTTP status. A JSON API
    must be judged that way: its body legitimately contains phrases like
    "blocked" or "rate limit" inside *result text*, and scanning it would retry,
    then discard, a perfectly good result set.
    """
    delays = list(RETRY_DELAYS) + [None]
    for delay in delays:
        response = _fetch(url, **kwargs)
        status = getattr(response, "status_code", 200)
        text = (getattr(response, "text", "") or "") if throttle_markers else ""
        if not looks_throttled(status, text):
            response.raise_for_status()
            return response
        if delay is not None:
            _sleep(delay)
    raise SearchThrottled(f"search provider throttled after {MAX_ATTEMPTS} attempts")


def duckduckgo(query: str, count: int) -> list[WebHit]:
    """Scrape the HTML endpoint. No API key, no quota to configure."""
    response = fetch_with_retry(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": settings.user_agent()},
        timeout=settings.search_timeout(),
        follow_redirects=True,
    )

    soup = BeautifulSoup(response.text, "html.parser")
    hits: list[WebHit] = []
    seen: set[str] = set()
    for block in soup.select(".result"):
        if len(hits) >= count:
            break
        link = block.select_one(".result__a")
        if not link:
            continue
        url = resolve_redirect(link.get("href", ""))
        if not url or is_ad_url(url):
            continue
        key = dedupe_url(url)
        if not key or key in seen:
            continue
        seen.add(key)
        snippet = block.select_one(".result__snippet")
        hits.append(
            WebHit(
                title=link.get_text(" ", strip=True),
                url=url,
                snippet=snippet.get_text(" ", strip=True) if snippet else "",
                provider="duckduckgo",
            )
        )
    return dedupe_hits(hits)


def searxng_search_url(instance: str) -> str:
    """``<base>/search``, tolerating a base that already ends with ``/search``."""
    base = (instance or "").strip().rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/search") else f"{base}/search"


def _searxng_hits(payload, count: int) -> list[WebHit]:
    """Parse a SearXNG JSON payload. A bad shape is a failure, never empty success."""
    if not isinstance(payload, dict):
        raise SearchMalformed(
            f"searxng returned {type(payload).__name__}, expected a JSON object"
        )
    raw = payload.get("results")
    if raw is None:
        detail = str(payload.get("error") or "").strip()[:200]
        raise SearchMalformed(
            "searxng response has no results list"
            + (f" (provider said: {detail})" if detail else "")
        )
    if not isinstance(raw, list):
        raise SearchMalformed(
            f"searxng returned results of type {type(raw).__name__}, expected a list"
        )
    hits: list[WebHit] = []
    seen: set[str] = set()
    for item in raw:
        if len(hits) >= count:
            break
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url or is_ad_url(url):
            continue
        key = dedupe_url(url)
        if not key or key in seen:
            continue
        seen.add(key)
        title = item.get("title")
        content = item.get("content")
        hits.append(
            WebHit(
                title=title.strip() if isinstance(title, str) else "",
                url=url.strip(),
                snippet=content.strip() if isinstance(content, str) else "",
                provider="searxng",
            )
        )
    return dedupe_hits(hits)


def searxng(query: str, count: int, endpoint: str | None = None) -> list[WebHit]:
    """Query a self-hosted SearXNG instance's JSON API."""
    instance = (endpoint or settings.searxng_url()).strip().rstrip("/")
    url = searxng_search_url(instance)
    if not url:
        raise ValueError(
            "SearXNG needs an endpoint: set ATHENA_SEARXNG_URL "
            "(or SEARXNG_INSTANCE / SEARCH_FALLBACK_ENDPOINT)"
        )

    response = fetch_with_retry(
        url,
        params={"q": query, "format": "json", "language": "en"},
        headers={"User-Agent": settings.user_agent()},
        timeout=settings.search_timeout(),
        # JSON bodies quote arbitrary page text; the status code decides throttling.
        throttle_markers=False,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        # A 200 with an HTML error page is the usual shape of "the JSON API is
        # switched off" or "the instance is blocking us". Either way there are
        # no results, and no results must not be reported as an empty success.
        body = getattr(response, "text", "") or ""
        if looks_throttled(0, body):
            raise SearchThrottled(
                "searxng answered with a block/throttle page instead of JSON"
            ) from exc
        raise SearchMalformed(f"malformed JSON from searxng: {exc}") from exc
    return _searxng_hits(payload, count)


def web_search(
    query: str,
    max_results: int = 5,
    *,
    provider: str | None = None,
    endpoint: str | None = None,
) -> WebResult:
    """Search the web. Never raises: failures come back as ``ok=False``.

    ``provider``/``endpoint`` let an in-process caller (chat) use the search
    settings of the selected provider profile instead of the process environment.
    The MCP wrapper keeps calling the two positional arguments, so its contract
    is unchanged.
    """
    started = time.perf_counter()
    provider = (provider or settings.search_provider()).strip().lower()
    limit = max(1, min(int(max_results), 20))

    def result(actual_provider: str | None = None, **kwargs) -> WebResult:
        return WebResult(
            query=query,
            provider=actual_provider or provider,
            max_results=limit,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            **kwargs,
        )

    if not (query or "").strip():
        # An empty query would return an arbitrary page of provider results that
        # look like evidence. Refuse it before any network call.
        return result(ok=False, error="query must not be empty")

    if provider == "disabled":
        return result(ok=False, error="web search is disabled (SEARCH_PROVIDER=disabled)")

    def caller_for(name: str, target_endpoint: str | None):
        if name == "duckduckgo":
            return lambda: duckduckgo(query, limit)  # noqa: E731 - tiny adapter
        if name == "searxng":
            return lambda: searxng(query, limit, endpoint=target_endpoint)  # noqa: E731
        return None

    primary = caller_for(provider, endpoint)
    if primary is None:
        return result(
            ok=False,
            error=f"unsupported SEARCH_PROVIDER {provider!r} "
            f"(use one of ['duckduckgo', 'searxng'] or disabled)",
        )

    fallback = settings.search_fallback_provider()
    fallback_endpoint = settings.search_fallback_endpoint()
    fallback_caller = (
        caller_for(fallback, fallback_endpoint)
        if fallback and fallback != provider
        else None
    )
    if fallback == "searxng" and not fallback_endpoint:
        fallback_caller = None

    def attribute(name: str, hits: list[WebHit]) -> list[WebHit]:
        """Stamp the hit's own provider, then dedupe and cap the result list."""
        for hit in hits:
            if not hit.provider:
                hit.provider = name
        return dedupe_hits(hits)[:limit]

    def try_provider(name: str, caller) -> tuple[list[WebHit] | None, str | None]:
        """Return (hits, error). ``None`` hits means the provider did not answer."""
        if not _is_healthy(name, query):
            return None, (
                f"{name} is cooling down after throttling; "
                + (
                    "trying configured fallback"
                    if fallback_caller is not None else "no fallback configured"
                )
            )
        try:
            hits = caller()
        except SearchThrottled as exc:
            _LAST_ERROR[name] = classify_error(exc)
            _mark_unhealthy(name, query)
            return None, f"{name} throttled"
        except Exception as exc:  # noqa: BLE001 - network failure is a result
            _LAST_ERROR[name] = classify_error(exc)
            _mark_unhealthy(name, query)
            return None, f"{name}: {type(exc).__name__}: {exc}"
        if not hits:
            _LAST_ERROR.pop(name, None)
            return [], None
        _LAST_ERROR.pop(name, None)
        return attribute(name, hits), None

    primary_hits, primary_error = try_provider(provider, primary)
    if primary_hits:
        return result(results=primary_hits)
    if fallback_caller is not None:
        fallback_hits, fallback_error = try_provider(fallback, fallback_caller)
        if fallback_hits:
            return result(
                actual_provider=fallback,
                results=fallback_hits,
                note=(
                    f"primary provider ({provider}) unavailable; "
                    f"fallback provider ({fallback}) used"
                ),
            )
        return result(
            ok=False,
            error=(
                f"search failed: {primary_error or 'no primary results'}; "
                f"fallback failed: {fallback_error or 'no fallback results'}"
            ),
        )
    if primary_error:
        if primary_error == f"{provider} throttled":
            primary_error = (
                "search provider throttled; no fallback configured "
                "(set ATHENA_SEARXNG_URL to enable the optional SearXNG fallback)"
            )
        return result(ok=False, error=primary_error)
    return result(results=[], note="no results for this query")
