"""SSRF-guarded page fetch for the researcher's web MCP server.

Search returns titles and snippets. This module reads a selected public page so
claims can be grounded in the page text instead of a search-result summary.

Security model:
- Only ``http``/``https`` URLs, ports 80/443, no URL credentials.
- Resolve every destination and reject non-global addresses.
- Validate every redirect target and resolve it again immediately before use.
- No ambient proxy, cookie jar, auth header, or internal header is forwarded.
- A total monotonic deadline spans redirects and body streaming; each hop gets
  only the remaining time. The per-read timeout is also the remaining time.
- Hard response-size limits; only text-like content types.
- Canonical URLs are accepted only on the final hostname; they are metadata and
  are never fetched.

DNS limitation: the address is resolved and validated immediately before each
request, but the HTTP client resolves again when it connects. A rebinding race is
therefore narrowed, not eliminated. A pinned-IP transport that connects only to a
validated address while preserving TLS hostname validation is the required
hardening step; this module does not claim to provide it.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from athena.tools import settings
from athena.tools.schemas import FetchResult

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}
ALLOWED_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "text/plain",
}
REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5
MAX_URL_LENGTH = 2048
MIN_CHARS = 1000
MAX_CHARS = 20000
MAX_TITLE = 300


class FetchBlocked(RuntimeError):
    """The destination is not safe to read."""


class FetchDeadline(RuntimeError):
    """The whole fetch exceeded its monotonic deadline."""


def _is_public_ip(raw: str) -> bool:
    address = (raw or "").split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    if (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_reserved
        or parsed.is_multicast
        or parsed.is_unspecified
        or not parsed.is_global
    ):
        return False
    return True


def _resolve_public(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise FetchBlocked("host could not be resolved") from exc
    addresses = []
    for info in infos:
        address = info[4][0]
        if not _is_public_ip(address):
            raise FetchBlocked("destination is not a public address")
        addresses.append(address)
    if not addresses:
        raise FetchBlocked("host resolved to no addresses")
    return addresses


def normalize_url(raw: str) -> tuple[str, str, int]:
    """Validate an absolute URL and return (normalized, host, port)."""
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise FetchBlocked("URL is empty or too long")
    if any(char in raw for char in ("\\", "\r", "\n", "\t", " ")):
        raise FetchBlocked("URL contains unsafe characters")
    try:
        parts = urlsplit(raw.strip())
    except ValueError as exc:
        raise FetchBlocked("URL is malformed") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise FetchBlocked("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise FetchBlocked("URL credentials are not allowed")
    try:
        host = (parts.hostname or "").strip().rstrip(".")
    except ValueError as exc:
        raise FetchBlocked("URL has no valid host") from exc
    if not host or "%" in host:
        raise FetchBlocked("URL has no valid host")
    try:
        port = parts.port
    except ValueError as exc:
        raise FetchBlocked("URL has an invalid port") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if port not in ALLOWED_PORTS:
        raise FetchBlocked("only ports 80 and 443 are allowed")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise FetchBlocked("URL host is not valid IDNA") from exc
    if host in {"localhost", "localhost.localdomain"} or host.endswith(
        (".localhost", ".local")
    ):
        raise FetchBlocked("local hostnames are not allowed")
    host_part = f"[{host}]" if ":" in host else host
    netloc = host_part if parts.port is None else f"{host_part}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, "")), host, port


def _validate_destination(url: str) -> str:
    normalized, host, port = normalize_url(url)
    _resolve_public(host, port)
    return normalized


def _normalized_content_type(value: str) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _has_canonical(value) -> bool:
    if isinstance(value, str):
        return "canonical" in value.lower().split()
    return any(str(item).lower() == "canonical" for item in value or [])


def _normalized_host(value: str) -> str:
    host = (value or "").strip().rstrip(".").lower()
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _canonical_url(candidate: str, base_url: str) -> str:
    """Accept only HTTP(S), credential-free canonical URLs on the final host."""
    try:
        parsed = urlsplit(candidate)
        base = urlsplit(base_url)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ALLOWED_SCHEMES or parsed.username or parsed.password:
        return ""
    if _normalized_host(parsed.hostname or "") != _normalized_host(base.hostname or ""):
        return ""
    return candidate


def _extract_html(html: str, base_url: str, max_chars: int) -> tuple[str, str, str, bool]:
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.title:
        title = " ".join(soup.title.get_text(" ", strip=True).split())[:MAX_TITLE]

    canonical = ""
    link = soup.find("link", rel=_has_canonical)
    if link and link.get("href"):
        canonical = _canonical_url(urljoin(base_url, link["href"]), base_url)

    for tag in soup(
        ["script", "style", "noscript", "svg", "form", "header", "footer", "nav", "head"]
    ):
        tag.decompose()

    lines = []
    for line in soup.get_text("\n").splitlines():
        cleaned = re.sub(r"[ \t\f\v]+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    truncated = len(text) > max_chars
    return title, canonical, text[:max_chars].rstrip(), truncated


def _extract_plain(text: str, base_url: str, max_chars: int) -> tuple[str, str, str, bool]:
    cleaned = (text or "").strip()
    truncated = len(cleaned) > max_chars
    return "", base_url, cleaned[:max_chars].rstrip(), truncated


def _failure(
    requested_url: str,
    status: str,
    started: float,
    *,
    error: str,
    final_url: str = "",
    status_code: int | None = None,
    content_type: str = "",
) -> FetchResult:
    return FetchResult(
        requested_url=requested_url,
        final_url=final_url or requested_url,
        canonical_url="",
        content_type=content_type,
        status=status,
        status_code=status_code,
        text="",
        truncated=False,
        ok=False,
        error=(error or status)[:300],
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _read_limited(
    response: httpx.Response, max_bytes: int, deadline: float
) -> tuple[bytes | None, bool]:
    if time.monotonic() >= deadline:
        raise FetchDeadline
    length = response.headers.get("content-length")
    if length:
        try:
            if int(length) > max_bytes:
                return None, True
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        if time.monotonic() >= deadline:
            raise FetchDeadline
        total += len(chunk)
        if total > max_bytes:
            return None, True
        chunks.append(chunk)
    if time.monotonic() >= deadline:
        raise FetchDeadline
    return b"".join(chunks), False


def _fetch_page(
    url: str,
    max_chars: int | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float | None = None,
    total_timeout: float | None = None,
) -> FetchResult:
    """Fetch and extract one page. Never raises; failures are results."""
    requested_url = str(url or "")
    limit = settings.fetch_max_chars() if max_chars is None else int(max_chars)
    limit = max(MIN_CHARS, min(MAX_CHARS, limit))
    max_bytes = settings.fetch_max_bytes()
    call_timeout = settings.fetch_timeout() if timeout is None else timeout
    total = settings.fetch_total_timeout() if total_timeout is None else total_timeout
    started = time.perf_counter()
    deadline = time.monotonic() + max(float(total), 0.001)
    current = requested_url

    try:
        for _ in range(MAX_REDIRECTS + 1):
            if time.monotonic() >= deadline:
                raise FetchDeadline
            remaining = deadline - time.monotonic()
            request_timeout = max(0.001, min(call_timeout, remaining))
            current = _validate_destination(current)
            headers = {
                "User-Agent": settings.user_agent(),
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
            }
            try:
                with httpx.Client(
                    transport=transport,
                    follow_redirects=False,
                    trust_env=False,
                    timeout=httpx.Timeout(
                        request_timeout, connect=min(5.0, request_timeout)
                    ),
                    headers=headers,
                ) as client:
                    with client.stream("GET", current) as response:
                        if time.monotonic() >= deadline:
                            raise FetchDeadline
                        code = response.status_code
                        if code in REDIRECT_CODES:
                            location = response.headers.get("location")
                            if not location:
                                return _failure(
                                    requested_url, "http_error", started,
                                    error="redirect had no Location header",
                                    final_url=current, status_code=code,
                                )
                            current = urljoin(current, location)
                            continue

                        content_type = _normalized_content_type(
                            response.headers.get("content-type", "")
                        )
                        if not 200 <= code < 300:
                            return _failure(
                                requested_url, "http_error", started,
                                error=f"HTTP {code}", final_url=current,
                                status_code=code, content_type=content_type,
                            )
                        if content_type not in ALLOWED_CONTENT_TYPES:
                            return _failure(
                                requested_url, "unsupported_content_type", started,
                                error=f"unsupported content type: {content_type or 'missing'}",
                                final_url=current, status_code=code,
                                content_type=content_type,
                            )
                        body, oversized = _read_limited(response, max_bytes, deadline)
                        if oversized or body is None:
                            return _failure(
                                requested_url, "too_large", started,
                                error=f"response exceeded {max_bytes} bytes",
                                final_url=current, status_code=code,
                                content_type=content_type,
                            )
                        if time.monotonic() >= deadline:
                            raise FetchDeadline
                        encoding = response.charset_encoding or "utf-8"
                        decoded = body.decode(encoding, "replace")
                        if content_type == "text/plain":
                            title, canonical, text, truncated = _extract_plain(
                                decoded, current, limit
                            )
                        else:
                            title, canonical, text, truncated = _extract_html(
                                decoded, current, limit
                            )
                        status = "ok" if text else "empty"
                        return FetchResult(
                            requested_url=requested_url,
                            final_url=current,
                            canonical_url=canonical or current,
                            title=title,
                            content_type=content_type,
                            status=status,
                            status_code=code,
                            text=text,
                            truncated=truncated,
                            ok=True,
                            error=None,
                            elapsed_ms=(time.perf_counter() - started) * 1000,
                        )
            except FetchDeadline:
                raise
            except httpx.TimeoutException:
                return _failure(
                    requested_url, "timeout", started,
                    error=f"fetch timed out after {request_timeout}s",
                    final_url=current,
                )
            except httpx.HTTPError as exc:
                return _failure(
                    requested_url, "error", started,
                    error=f"{type(exc).__name__}: {exc}",
                    final_url=current,
                )
        return _failure(
            requested_url, "error", started,
            error=f"more than {MAX_REDIRECTS} redirects",
            final_url=current,
        )
    except FetchDeadline:
        return _failure(
            requested_url, "timeout", started,
            error=f"fetch exceeded total deadline of {total}s",
            final_url=current,
        )
    except FetchBlocked as exc:
        return _failure(requested_url, "blocked", started, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - a failed fetch is a result, not a crash
        return _failure(
            requested_url, "error", started,
            error=f"{type(exc).__name__}: {exc}",
            final_url=current,
        )


def fetch_page(url: str, max_chars: int = 6000) -> FetchResult:
    """Read a public web page and return bounded, extractable text.

    Args:
        url: Absolute public http/https URL.
        max_chars: Maximum readable characters to return (1000-20000).
    """
    return _fetch_page(url, max_chars)
