"""Editable persistent personal memory API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from athena.api.guards import guard
from athena.chat import personal_memory as memory

router = APIRouter(prefix="/api/memory", tags=["memory"])


class MemoryIn(BaseModel):
    category: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=memory.MAX_CONTENT)


class MemoryPatch(BaseModel):
    category: str | None = Field(default=None, max_length=32)
    content: str | None = Field(default=None, max_length=memory.MAX_CONTENT)


class MemorySettingsIn(BaseModel):
    enabled: bool


@router.get("")
def get_memory(request: Request) -> dict:
    guard(request)
    return {"items": memory.list_items(), **memory.settings()}


@router.put("/settings")
def put_memory_settings(payload: MemorySettingsIn, request: Request) -> dict:
    guard(request)
    return {"enabled": memory.set_enabled(payload.enabled)}


@router.post("", status_code=201)
def create_memory(payload: MemoryIn, request: Request) -> dict:
    guard(request)
    try:
        return {"item": memory.add_item(payload.category, payload.content)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/{item_id}")
def edit_memory(item_id: str, payload: MemoryPatch, request: Request) -> dict:
    guard(request)
    try:
        item = memory.update_item(item_id, category=payload.category, content=payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="memory item not found")
    return {"item": item}


@router.delete("/{item_id}")
def remove_memory(item_id: str, request: Request) -> dict:
    guard(request)
    if not memory.delete_item(item_id):
        raise HTTPException(status_code=404, detail="memory item not found")
    return {"deleted": True, "id": item_id}
