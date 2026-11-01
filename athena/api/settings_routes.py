"""Profile CRUD and the security gates for a localhost API that stores keys.

Two defences, both cheap and both tested:

* ``Host``/``Origin`` checks - a page on another site must not be able to POST
  here with the user's browser as the transport (drive-by request).
* Keys are written straight to the vault and never echoed back.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from athena.api.guards import guard
from athena.settings import defaults
from athena.settings import profiles as store
from athena.settings.profiles import Profile, ProfileError, validate_api_base
from athena.settings.vault import VaultError, default_vault

router = APIRouter(prefix="/api/settings")

# Kept as the private name used by every handler below; one shared policy now
# covers settings, chat and library writes.
_guard = guard


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

class ActivateIn(BaseModel):
    profile_id: str


@router.post("/activate")
def activate_profile(payload: ActivateIn, request: Request) -> dict:
    _guard(request)
    from athena.api.activation import ActivationError, activate

    try:
        return activate(payload.profile_id)
    except ActivationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/profiles/{profile_id}/test")
async def test_profile_endpoint(profile_id: str, request: Request) -> dict:
    _guard(request)
    from athena.api import probe
    from athena.api.activation import profile_env
    from athena.settings.profiles import ProfileError

    if store.find(profile_id) is None:
        raise HTTPException(status_code=404, detail=f"no profile {profile_id!r}")
    try:
        env = profile_env(profile_id)
    except ProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return await probe.test_profile(profile_id, env)
    except probe.Busy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - always answer with JSON
        probe.log.warning("test endpoint failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500,
                            detail=f"test failed: {type(exc).__name__}") from exc

class AboutMeIn(BaseModel):
    about_me: str = Field(default="", max_length=800)


@router.get("/about-me")
def get_about_me(request: Request) -> dict:
    _guard(request)
    from athena.settings.preferences import about_me

    return {"about_me": about_me()}


@router.put("/about-me")
def put_about_me(payload: AboutMeIn, request: Request) -> dict:
    _guard(request)
    from athena.settings.preferences import set_about_me

    try:
        return {"about_me": set_about_me(payload.about_me)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class DefaultsIn(BaseModel):
    chat_profile_id: str = Field(default="", max_length=64)


@router.get("/defaults")
def get_defaults(request: Request) -> dict:
    _guard(request)
    return {
        "chat_profile_id": defaults.chat_profile_id(),
        "env_profile": defaults.ENV_PROFILE,
        "active": store.active_id(),
    }


@router.put("/defaults")
def put_defaults(payload: DefaultsIn, request: Request) -> dict:
    _guard(request)
    value = (payload.chat_profile_id or "").strip()
    if value and value != defaults.ENV_PROFILE and store.find(value) is None:
        raise HTTPException(status_code=404, detail=f"no profile {value!r}")
    defaults.set_chat_profile(value)
    return {"chat_profile_id": value}


class ModelsIn(BaseModel):
    profile_id: str = Field(default="", max_length=64)
    api_base: str = Field(default="", max_length=500)
    llm_key: str | None = None


def _model_ids(payload) -> list[str]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("data") or payload.get("models") or []
    else:
        rows = []
    found: list[str] = []
    for row in rows:
        if isinstance(row, str):
            value = row
        elif isinstance(row, dict):
            value = str(row.get("id") or row.get("name") or "")
        else:
            value = str(getattr(row, "id", "") or "")
        value = value.strip()
        if value and value not in found:
            found.append(value)
        if len(found) >= 400:
            break
    return found


@router.post("/models")
async def discover_models(payload: ModelsIn, request: Request) -> dict:
    _guard(request)
    import httpx

    profile_id = (payload.profile_id or "").strip()
    key = payload.llm_key or ""
    base = (payload.api_base or "").strip()
    if profile_id:
        profile = store.find(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"no profile {profile_id!r}")
        base = base or profile.api_base
        if not key:
            key = default_vault().get(store.key_name(profile_id, "llm")) or ""
    try:
        base = validate_api_base(base)
    except ProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not base:
        raise HTTPException(status_code=422, detail="api_base is required for model discovery")
    if not key:
        from urllib.parse import urlparse

        if (urlparse(base).hostname or "") not in {"localhost", "127.0.0.1", "::1"}:
            return {"models": [], "count": 0, "supported": False, "error": "API key required."}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(
            timeout=15, trust_env=False, follow_redirects=False
        ) as client:
            response = await client.get(base.rstrip("/") + "/models", headers=headers)
            response.raise_for_status()
            model_ids = _model_ids(response.json())
    except Exception as exc:  # noqa: BLE001 - discovery unavailable is a result
        return {
            "models": [],
            "count": 0,
            "supported": False,
            "error": f"Model discovery unavailable: {type(exc).__name__}",
        }
    return {"models": model_ids, "count": len(model_ids), "supported": bool(model_ids)}


@router.get("/mcp")
def mcp_status(request: Request) -> dict:
    _guard(request)
    from types import SimpleNamespace

    from athena.chat import tools
    from athena.chat.target import search_settings

    provider, endpoint = search_settings()
    rows = tools.catalog(
        SimpleNamespace(search_provider=provider, search_endpoint=endpoint)
    )
    servers = {
        "athena-rag": {"id": "athena-rag", "name": "Athena RAG", "module": "athena.tools.rag_server"},
        "athena-web": {"id": "athena-web", "name": "Athena Web", "module": "athena.tools.web_server"},
        "athena-calc": {"id": "athena-calc", "name": "Athena Calc", "module": "athena.tools.calc_server"},
    }
    for server in servers.values():
        server["tools"] = [
            {
                "name": row["id"],
                "label": row["label"],
                "description": row["description"],
                "available": row["available"],
                "reason": row["reason"],
                "readonly": row["readonly"],
            }
            for row in rows
            if row["server"] == server["id"]
        ]
    try:
        import importlib.util

        available = importlib.util.find_spec("mcp") is not None
    except Exception:  # noqa: BLE001 - the package is an optional runtime
        available = False
    return {
        "servers": list(servers.values()),
        "mcp_available": available,
        "note": (
            "athena MAS ships these fixed local stdio MCP servers. Adding arbitrary "
            "MCP servers is not supported because it would execute arbitrary commands."
        ),
    }