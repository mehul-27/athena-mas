"""One Host/Origin policy for every local write endpoint.

The demo API has no authentication. The browser is the only intended client,
so a write from another site (or a request carrying a Host we do not serve) is
refused. This is not a substitute for real auth; it is the boundary that keeps
a localhost service from being driven by an arbitrary page.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException, Request

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "testserver"}


def guard(request: Request) -> None:
    """Reject a Host we do not serve and any cross-site write."""
    host = (request.headers.get("host") or "").split(":")[0]
    if host and host not in ALLOWED_HOSTS:
        raise HTTPException(status_code=400, detail="unexpected Host header")

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin:
            # Compare hostnames only: the page may be served on any local port.
            origin_host = urlparse(origin).hostname or ""
            if origin_host not in ALLOWED_HOSTS:
                raise HTTPException(status_code=403, detail="cross-origin request refused")
