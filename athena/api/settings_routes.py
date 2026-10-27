"""Profile CRUD and the security gates for a localhost API that stores keys.

Two defences, both cheap and both tested:

* ``Host``/``Origin`` checks - a page on another site must not be able to POST
  here with the user's browser as the transport (drive-by request).
* Keys are written straight to the vault and never echoed back.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from athena.settings import profiles as store
from athena.settings.profiles import Profile, ProfileError
from athena.settings.vault import VaultError, default_vault

router = APIRouter(prefix="/api/settings")
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "testserver"}


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    preset: str = "custom"
    api_base: str = ""
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str = ""
    search_provider: str = "duckduckgo"
    search_endpoint: str = ""
    llm_key: str | None = None
    search_key: str | None = None


def _guard(request: Request) -> None:
    """Reject a Host we do not serve and any cross-site write."""
    host = (request.headers.get("host") or "").split(":")[0]
    if host and host not in ALLOWED_HOSTS:
        raise HTTPException(status_code=400, detail="unexpected Host header")

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse

            # Compare hostnames only: the page may be served on any local port.
            origin_host = urlparse(origin).hostname or ""
            if origin_host not in ALLOWED_HOSTS:
                raise HTTPException(status_code=403, detail="cross-origin request refused")


@router.get("/profiles")
def list_profiles(request: Request) -> dict:
    _guard(request)
    return {"profiles": [profile.public() for profile in store.load_profiles()],
            "active": store.active_id()}


@router.get("/presets")
def list_presets(request: Request) -> dict:
    _guard(request)
    return {"presets": store.presets()}


@router.post("/profiles")
def create_profile(payload: ProfileIn, request: Request) -> dict:
    _guard(request)
    return _save(payload, Profile(**payload.model_dump(exclude={"llm_key", "search_key"})))


@router.put("/profiles/{profile_id}")
def update_profile(profile_id: str, payload: ProfileIn, request: Request) -> dict:
    _guard(request)
    if store.find(profile_id) is None:
        raise HTTPException(status_code=404, detail=f"no profile {profile_id!r}")
    profile = Profile(id=profile_id, **payload.model_dump(exclude={"llm_key", "search_key"}))
    return _save(payload, profile)


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str, request: Request) -> dict:
    _guard(request)
    store.delete(profile_id)
    return {"deleted": profile_id}


@router.get("/active")
def get_active(request: Request) -> dict:
    _guard(request)
    return {"profile_id": store.active_id()}


@router.put("/active")
def put_active(payload: dict, request: Request) -> dict:
    _guard(request)
    profile_id = str(payload.get("profile_id") or "")
    if profile_id and store.find(profile_id) is None:
        raise HTTPException(status_code=404, detail=f"no profile {profile_id!r}")
    store.set_active(profile_id)
    return {"profile_id": profile_id}


def _save(payload: ProfileIn, profile: Profile) -> dict:
    try:
        store.upsert(profile)
        vault = default_vault()
        if payload.llm_key:
            vault.set(store.key_name(profile.id, "llm"), payload.llm_key)
        if payload.search_key:
            vault.set(store.key_name(profile.id, "search"), payload.search_key)
    except ProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except VaultError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return profile.public()
