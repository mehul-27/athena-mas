"""Local project workspaces and their references to existing Athena items."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from athena.api.guards import guard
from athena.chat.store import ChatStore

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])
ItemKind = Literal["chat", "report", "document", "reminder", "watchlist", "action"]


class WorkspaceIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=2400)


class WorkspaceItemIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    kind: ItemKind
    ref: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=240)


@router.get("")
def list_workspaces(request: Request) -> dict:
    guard(request)
    return {"workspaces": ChatStore().workspaces()}


@router.post("", status_code=201)
def create_workspace(payload: WorkspaceIn, request: Request) -> dict:
    guard(request)
    return {"workspace": ChatStore().save_workspace(payload.name, payload.notes)}


@router.patch("/{workspace_id}")
def edit_workspace(workspace_id: str, payload: WorkspaceIn, request: Request) -> dict:
    guard(request)
    workspace = ChatStore().save_workspace(payload.name, payload.notes, workspace_id)
    if workspace is None:
        raise HTTPException(404, "Project not found")
    return {"workspace": workspace}


@router.delete("/{workspace_id}")
def delete_workspace(workspace_id: str, request: Request) -> dict:
    guard(request)
    if not ChatStore().delete_workspace(workspace_id):
        raise HTTPException(404, "Project not found")
    return {"deleted": True}


@router.get("/{workspace_id}/items")
def list_workspace_items(workspace_id: str, request: Request) -> dict:
    guard(request)
    items = ChatStore().workspace_items(workspace_id)
    if items is None:
        raise HTTPException(404, "Project not found")
    return {"items": items}


@router.post("/{workspace_id}/items")
def link_workspace_item(workspace_id: str, payload: WorkspaceItemIn, request: Request) -> dict:
    guard(request)
    try:
        ChatStore().link_workspace_item(workspace_id, payload.kind, payload.ref, payload.label)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"linked": True}


@router.delete("/{workspace_id}/items/{kind}/{ref}")
def unlink_workspace_item(workspace_id: str, kind: ItemKind, ref: str, request: Request) -> dict:
    guard(request)
    if not ChatStore().unlink_workspace_item(workspace_id, kind, ref):
        raise HTTPException(404, "Item not in this project")
    return {"unlinked": True}
