"""Provider diagnostics: what is configured, what is reachable, what failed last.

The point of this module is what it *does not* contain. A status report is meant
to be printed in a terminal, returned by an endpoint, pasted into an issue or fed
to a chat tool, so it must be safe in all of those places:

- no API key, token, cookie or Authorization header value is ever included, and
  no key is included even as a prefix or length;
- an endpoint URL is redacted: userinfo, query string and fragment are dropped,
  because ``http://user:secret@host:8080`` is a credential too;
- the "last error" is a *class* from a short vocabulary
  (``throttled``, ``timeout``, ``network_error``, ``http_error``,
  ``malformed_response``, ``invalid_configuration``, ``error``), never a raw
  exception message that might quote a response body or a URL with a key in it.

Reachability probing is opt-in (``probe=True``) and bounded: the default report
performs no network I/O, so importing and calling it in a UI loop is free.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit, urlunsplit

from athena.tools import settings
from athena.tools.web import search as search_adapter

# Suffixes that mark a value as a secret once somebody builds a dict from env.
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_PROBE_TIMEOUT_S = 4.0


def redact_url(raw: str) -> str:
    """An endpoint URL with credentials, query and fragment removed."""
    value = (raw or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return "(unparseable url)"
    if not parts.scheme or not parts.netloc:
        return "(unparseable url)"
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port else host
    # The path is kept (it identifies a template), never the query or fragment.
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return name == "LLM_API_KEY" or any(marker in upper for marker in _SECRET_MARKERS)


def safe_env_snapshot(names: list[str]) -> dict[str, str]:
    """Selected environment values with every secret name replaced by ``set``."""
    snapshot: dict[str, str] = {}
    for name in names:
        value = os.getenv(name) or ""
        if _is_secret_name(name):
            snapshot[name] = "set" if value else "missing"
        elif "URL" in name.upper() or "ENDPOINT" in name.upper():
            snapshot[name] = redact_url(value) or "missing"
        else:
            snapshot[name] = value or "missing"
    return snapshot


def _probe_searxng(endpoint: str) -> tuple[bool | None, str]:
    """One bounded GET to ``<endpoint>/search``. Returns (reachable, error class).

    Reachable means "the JSON API answered with a JSON object", not merely "TCP
    connected": an instance whose ``format=json`` is disabled is configured but
    not usable, and the report must say so.
    """
    url = search_adapter.searxng_search_url(endpoint)
    if not url:
        return None, "invalid_configuration"
    try:
        response = search_adapter._fetch(  # noqa: SLF001 - same package, test seam
            url,
            params={"q": "athena", "format": "json"},
            headers={"User-Agent": settings.user_agent()},
            timeout=_PROBE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - a probe failure is data
        return False, search_adapter.classify_error(exc)
    status = getattr(response, "status_code", 0) or 0
    if status in search_adapter.THROTTLE_STATUS:
        return False, "throttled"
    if status >= 400:
        return False, "http_error"
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - an HTML body is a malformed answer
        return False, "malformed_response"
    if not isinstance(payload, dict):
        return False, "malformed_response"
    return True, ""


def _probe_duckduckgo() -> tuple[bool | None, str]:
    """One bounded GET to the DuckDuckGo HTML endpoint.

    Deliberately a HEAD-shaped cheap request: the HTML endpoint answers ``GET``
    only, so this asks for the smallest possible page and discards it.
    """
    try:
        response = search_adapter._fetch(  # noqa: SLF001 - same package, test seam
            "https://html.duckduckgo.com/html/",
            params={"q": "athena"},
            headers={"User-Agent": settings.user_agent()},
            timeout=_PROBE_TIMEOUT_S,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001 - a probe failure is data
        return False, search_adapter.classify_error(exc)
    status = getattr(response, "status_code", 0) or 0
    if search_adapter.looks_throttled(status, getattr(response, "text", "") or ""):
        return False, "throttled"
    if status >= 400:
        return False, "http_error"
    return True, ""


def search_providers(probe: bool = False) -> list[dict]:
    """One row per search provider: role, configuration, reachability, last error.

    ``reachable`` is ``None`` when no probe was requested (the default) so a
    report never claims a check that did not happen.
    """
    primary = settings.search_provider()
    fallback = settings.search_fallback_provider()
    fallback_endpoint = settings.search_fallback_endpoint()
    searxng_endpoint = settings.searxng_url()

    rows: list[dict] = []
    for name in search_adapter.PROVIDERS:
        roles = []
        if name == primary:
            roles.append("primary")
        if name == fallback and fallback != primary:
            roles.append("fallback")
        endpoint = searxng_endpoint if name == "searxng" else ""
        if name == "searxng" and "fallback" in roles:
            endpoint = fallback_endpoint or endpoint
        configured = bool(roles)
        note = "" if roles else "not configured"
        if name == "searxng" and roles and not endpoint:
            # Selecting SearXNG without a URL is a configuration error, not a
            # reachable provider: say so instead of probing an empty string.
            configured = False
            note = "endpoint missing (set ATHENA_SEARXNG_URL)"
        reachable: bool | None = None
        probe_error = ""
        if probe and configured:
            if name == "searxng":
                reachable, probe_error = _probe_searxng(endpoint)
            else:
                reachable, probe_error = _probe_duckduckgo()
        rows.append(
            {
                "provider": name,
                "roles": roles,
                "configured": configured,
                "endpoint": redact_url(endpoint) if name == "searxng" else "",
                "reachable": reachable,
                "last_error_class": probe_error
                or search_adapter.last_error_class(name),
                "note": note,
            }
        )
    return rows


def credential_status() -> dict:
    """Which LLM provider is configured and whether a key is present.

    Presence only: the value is never read into this report, and ``configured``
    is computed from truthiness, not from the key's contents. The key is looked
    up in the environment first (``.env`` is loaded by ``athena.config.llm``) and
    then in the encrypted settings vault of the active profile, so an
    activated profile is reported correctly without the key ever leaving its
    store. ``key_source`` is a location name, never a value.
    """
    from athena.config import llm as llm_config

    prefix = llm_config.provider_prefix()
    key_var = llm_config.provider_key_var() or ""
    source = ""
    key = os.getenv("LLM_API_KEY") or (os.getenv(key_var) if key_var else "") or ""
    if key:
        source = "env"
    if not key:
        try:
            from athena.api.activation import active_env

            active = active_env() or {}
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            active = {}
        if active.get("LLM_API_KEY"):
            key = active["LLM_API_KEY"]
            source = "settings_vault"
    return {
        "provider": prefix,
        "model": os.getenv("LLM_MODEL") or llm_config.DEFAULT_MODEL,
        "key_env_var": key_var or "LLM_API_KEY",
        "configured": bool(key),
        "key_source": source or "missing",
        "api_base": redact_url(llm_config.api_base()),
        "reasoning_effort": llm_config.reasoning_effort(),
    }


def env_summary(env) -> dict:
    """Summarize an environment *mapping* (e.g. a child-process env) safely.

    Used by the launcher, which has already merged the active profile into a dict
    and wants to prove what it is about to start without echoing a key.
    """
    def value(name: str) -> str:
        return str(env.get(name) or "").strip()

    key_present = bool(value("LLM_API_KEY")) or bool(value("GROQ_API_KEY"))
    searxng_url = value("ATHENA_SEARXNG_URL") or value("SEARXNG_INSTANCE")
    explicit_fallback = value("SEARCH_FALLBACK_PROVIDER").lower()
    if explicit_fallback in {"none", "off", "disabled"}:
        fallback = "none"
    else:
        # ATHENA_SEARXNG_URL is itself the opt-in for the optional fallback.
        fallback = explicit_fallback or ("searxng" if searxng_url else "none")
    return {
        "llm_model": value("LLM_MODEL") or "(default)",
        "llm_key": "set" if key_present else "missing",
        "search_provider": value("SEARCH_PROVIDER") or "duckduckgo",
        "searxng_url": redact_url(searxng_url) or "missing",
        "search_fallback": fallback,
        "search_fallback_endpoint": redact_url(
            value("SEARCH_FALLBACK_ENDPOINT") or searxng_url
        ) or "missing",
    }


def status_report(probe: bool = False) -> dict:
    """The whole provider picture, safe to print, log, or serve as JSON."""
    return {
        "search": {
            "primary": settings.search_provider(),
            "fallback": settings.search_fallback_provider(),
            "providers": search_providers(probe=probe),
        },
        "llm": credential_status(),
        "embedding_provider": settings.embedding_provider(),
        "env": safe_env_snapshot(
            [
                "SEARCH_PROVIDER",
                "ATHENA_SEARXNG_URL",
                "SEARXNG_INSTANCE",
                "SEARCH_FALLBACK_PROVIDER",
                "SEARCH_FALLBACK_ENDPOINT",
                "SEARCH_TIMEOUT",
                "LLM_MODEL",
                "LLM_API_BASE",
                "LLM_API_KEY",
            ]
        ),
    }


def leaks_secret(report: dict, secrets: list[str]) -> list[str]:
    """Any of ``secrets`` that appears anywhere in the report (should be none)."""
    blob = json.dumps(report, default=str)
    return [secret for secret in secrets if secret and secret in blob]


def main(argv: list[str] | None = None) -> int:
    """``python -m athena.tools.diagnostics [--probe]`` -> JSON on stdout."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    report = status_report(probe="--probe" in args)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
