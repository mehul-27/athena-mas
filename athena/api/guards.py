"""One Host/Origin policy for every write endpoint.

The demo API is meant to be the only client of itself; a write from another
site (or a request carrying a Host we do not serve) is refused. This is not a
substitute for real auth (see ``athena.api.auth``); it is the boundary that
keeps a service from being driven by an arbitrary page.

The Host allowlist is deployment-configurable. ``ATHENA_ALLOWED_HOSTS`` is a
comma-separated list of *additional* hosts, needed once the app answers on a
public name behind a proxy (Fly, Render, a tunnel):

    ATHENA_ALLOWED_HOSTS=athena-mas.fly.dev,athena.example.com

Wildcards are accepted as ``*.fly.dev`` and ``.fly.dev`` (both mean "that
domain and any subdomain"). Unset, the behaviour is exactly the local one from
before, so a checkout, the test client and the loopback A2A card keep working.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from fastapi import HTTPException, Request

ENV_ALLOWED_HOSTS = "ATHENA_ALLOWED_HOSTS"

# Unchanged default: a local checkout, the test client, and a loopback host.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "testserver"})


def _normalise(host: str) -> str:
    """Lowercase, strip a port-free IPv6 wrapper and a trailing dot."""
    return (host or "").strip().lower().strip("[]").rstrip(".")


def configured_hosts() -> set[str]:
    """The effective allowlist: the defaults plus ``ATHENA_ALLOWED_HOSTS``."""
    extra: set[str] = set()
    for item in (os.getenv(ENV_ALLOWED_HOSTS) or "").split(","):
        host = _normalise(item)
        if host:
            extra.add(host)
    return set(ALLOWED_HOSTS) | extra


def host_allowed(host: str, allowed: set[str] | None = None) -> bool:
    """Is ``host`` in the allowlist? Empty host means "not served"."""
    host = _normalise(host)
    if not host:
        return False
    allowed = configured_hosts() if allowed is None else allowed
    if host in allowed:
        return True
    for entry in allowed:
        if entry.startswith("*."):
            suffix = entry[2:]
        elif entry.startswith("."):
            suffix = entry[1:]
        else:
            continue
        if suffix and (host == suffix or host.endswith("." + suffix)):
            return True
    return False


def request_host(request: Request) -> str:
    """Host header without its port."""
    return _normalise((request.headers.get("host") or "").split(":")[0])


def guard(request: Request) -> None:
    """Reject a Host we do not serve and any cross-site write."""
    allowed = configured_hosts()
    host = request_host(request)
    if host and not host_allowed(host, allowed):
        raise HTTPException(status_code=400, detail="unexpected Host header")

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin:
            # Compare hostnames only: the page may be served on any port, and a
            # same-origin write is never a cross-site request.
            origin_host = _normalise(urlparse(origin).hostname or "")
            if origin_host and origin_host == host:
                return
            if not host_allowed(origin_host, allowed):
                raise HTTPException(
                    status_code=403, detail="cross-origin request refused"
                )
