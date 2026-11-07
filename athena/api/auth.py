"""Optional shared-token access control for the Athena API.

Off by default: with no ``ATHENA_ACCESS_TOKEN`` every request behaves exactly as
it did before, which is what a single-user localhost checkout wants. Set the
variable and the app is closed to everyone who does not present the token - the
API stores provider keys, spawns processes and spends tokens, so a public host
must never run without it.

A request is authorised when any of these carries the token:

* ``Authorization: Bearer <token>``               (scripts, curl, fetch)
* ``Authorization: Basic base64(<token>:)``       (``curl -u <token>:``)
  ... or ``base64(<user>:<token>)``, so any user name works
* ``X-Athena-Token: <token>``
* the ``athena_session`` cookie, which ``GET /login?token=<token>`` sets

Comparisons use :func:`secrets.compare_digest`, so a wrong token leaks nothing
about the right one through timing. The token is never logged and never echoed
back by any endpoint.

Exempt paths: ``/healthz`` and ``/readyz`` (the platform probe cannot send
headers) plus ``/login`` and ``/logout`` themselves. Static asset GETs
(``*.js``, ``*.css``, images, fonts) are exempt so the login page and shell can
load; every API response still requires the token.
"""

from __future__ import annotations

import base64
import binascii
import html
import os
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

ENV_TOKEN = "ATHENA_ACCESS_TOKEN"
COOKIE_NAME = "athena_session"
COOKIE_MAX_AGE_S = 60 * 60 * 24 * 30

EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/login", "/logout"})
# Assets only: ``.html`` is deliberately absent, because the app shell
# (``/`` and ``/index.html``) is what a signed-out visitor must not get.
STATIC_SUFFIXES = (
    ".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".ico", ".webp", ".woff", ".woff2", ".ttf", ".map",
)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

router = APIRouter()


def configured_token() -> str:
    """The shared token, or "" when access control is disabled."""
    return (os.getenv(ENV_TOKEN) or "").strip()


def auth_enabled() -> bool:
    return bool(configured_token())


def token_matches(candidate: str, expected: str | None = None) -> bool:
    """Constant-time token comparison (never raises on odd input)."""
    expected = configured_token() if expected is None else expected
    if not expected:
        return False
    try:
        return secrets.compare_digest(
            (candidate or "").encode("utf-8", "surrogatepass"),
            expected.encode("utf-8", "surrogatepass"),
        )
    except Exception:  # noqa: BLE001 - a malformed token is simply wrong
        return False


def _basic_candidate(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""
    user, sep, password = decoded.partition(":")
    if not sep:
        return decoded
    # curl -u <token>:  -> user holds the token; <user>:<token> also works.
    return password or user


def token_from_request(request: Request) -> str:
    """Every place a client may put the token, in one deterministic order."""
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    scheme = scheme.strip().lower()
    value = value.strip()
    if value and scheme == "bearer":
        return value
    if value and scheme == "basic":
        return _basic_candidate(value)
    extra = (request.headers.get("x-athena-token") or "").strip()
    if extra:
        return extra
    return (request.cookies.get(COOKIE_NAME) or "").strip()


def is_static_asset(request: Request) -> bool:
    return request.method in SAFE_METHODS and request.url.path.lower().endswith(
        STATIC_SUFFIXES
    )


def is_exempt(request: Request) -> bool:
    path = request.url.path
    if path in EXEMPT_PATHS:
        return True
    if request.method == "OPTIONS":
        # CORS preflight carries no credentials; the real request is still gated.
        return True
    return is_static_asset(request)


def authorised(request: Request, token: str | None = None) -> bool:
    token = configured_token() if token is None else token
    if not token:
        return True
    return token_matches(token_from_request(request), token)


def _wants_html(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


def denial(request: Request) -> Response:
    """401 for the API, a redirect to the login page for a browser page view."""
    if request.method in SAFE_METHODS and _wants_html(request) and (
        request.url.path in {"/", "/index.html"}
    ):
        return RedirectResponse(
            "/login?" + urlencode({"next": request.url.path}), status_code=303
        )
    return JSONResponse(
        status_code=401,
        content={"detail": "authentication required"},
        headers={"WWW-Authenticate": 'Bearer realm="athena"'},
    )


class AccessTokenMiddleware:
    """Pure ASGI middleware so SSE responses are never buffered."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = configured_token()
        if not token:
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if is_exempt(request) or authorised(request, token):
            await self.app(scope, receive, send)
            return
        await denial(request)(scope, receive, send)


_LOGIN_FORM = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Athena - sign in</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;
      font:16px/1.5 system-ui,sans-serif;background:#111;color:#eee}}
 form{{display:flex;gap:8px;flex-direction:column;min-width:min(320px,88vw)}}
 input,button{{font:inherit;padding:10px 12px;border-radius:8px;
      border:1px solid #444;background:#1b1b1b;color:inherit}}
 button{{border-color:#666;cursor:pointer}}
 p{{color:#aaa;margin:0 0 4px}}
</style></head><body>
<form method="get" action="/login">
 <p>Enter the access token for this Athena instance.</p>
 <input type="password" name="token" autocomplete="off" autofocus
        aria-label="Access token">
 <input type="hidden" name="next" value="{next_path}">
 <button type="submit">Continue</button>
</form></body></html>
"""


def _safe_next(value: str | None) -> str:
    """Only ever redirect back into this app."""
    candidate = (value or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    if "\\" in candidate or ":" in candidate.split("/")[1:2]:
        return "/"
    return candidate


def _cookie_secure(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-proto") or "").lower()
    return forwarded.split(",")[0].strip() == "https" or request.url.scheme == "https"


@router.get("/login", response_class=HTMLResponse)
def login(request: Request, token: str = "", next: str = "/"):
    """Token in, cookie out. A bare GET renders the form."""
    target = _safe_next(next)
    expected = configured_token()
    if not expected:
        # Access control is off; there is nothing to sign in to.
        return RedirectResponse(target, status_code=303)
    if not token:
        return HTMLResponse(
            _LOGIN_FORM.format(next_path=html.escape(target, quote=True)),
            headers={"Cache-Control": "no-store"},
        )
    if not token_matches(token, expected):
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Athena</title>"
            "<p>Invalid access token. <a href=\"/login\">Try again</a>.</p>",
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
