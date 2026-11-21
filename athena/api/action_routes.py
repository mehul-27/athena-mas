"""Reviewable local actions. Approving never performs an external dispatch."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from athena.actions.store import ActionKind, ActionStatus, ActionStore
from athena.api.guards import guard

router = APIRouter(prefix="/api/actions", tags=["actions"])


class ActionIn(BaseModel):
    kind: ActionKind
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)
    source: str = Field(default="", max_length=500)
    start_at: datetime | None = None
    end_at: datetime | None = None


def _store() -> ActionStore:
    return ActionStore()


@router.post("")
def create_action(payload: ActionIn, request: Request) -> dict:
    guard(request)
    if payload.kind == "calendar_event":
        if payload.start_at is None or payload.end_at is None:
            raise HTTPException(status_code=422, detail="calendar events require start_at before end_at")
        if payload.start_at.utcoffset() is None or payload.end_at.utcoffset() is None:
            raise HTTPException(status_code=422, detail="calendar timestamps must include a timezone")
        if payload.end_at <= payload.start_at:
            raise HTTPException(status_code=422, detail="calendar events require start_at before end_at")
    elif payload.start_at is not None or payload.end_at is not None:
        raise HTTPException(status_code=422, detail="start_at and end_at are only valid for calendar_event")
    try:
        data = payload.model_dump()
        for field in ("start_at", "end_at"):
            data[field] = data[field].isoformat() if data[field] is not None else ""
        action = _store().create(**data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"action": action}


@router.get("")
def list_actions(request: Request, status: ActionStatus | None = None) -> dict:
    guard(request)
    return {"actions": _store().list(status)}


@router.get("/{action_id}")
def get_action(action_id: str, request: Request) -> dict:
    guard(request)
    action = _store().get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    return {"action": action}


def _transition(action_id: str, status: Literal["approved", "canceled"]) -> dict:
    store = _store()
    action = store.transition(action_id, status)
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    if action["status"] not in {status, "pending"}:
        raise HTTPException(status_code=409, detail=f"action is already {action['status']}")
    return {"action": action}


@router.post("/{action_id}/approve")
def approve_action(action_id: str, request: Request) -> dict:
    guard(request)
    return _transition(action_id, "approved")


@router.post("/{action_id}/cancel")
def cancel_action(action_id: str, request: Request) -> dict:
    guard(request)
    return _transition(action_id, "canceled")


def _ics_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _ics_fold(line: str) -> str:
    parts = []
    segment = []
    size = 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 75:
            parts.append("".join(segment))
            segment = [" "]
            size = 1
        segment.append(char)
        size += width
    parts.append("".join(segment))
    return "\r\n".join(parts)


@router.get("/{action_id}/export")
def export_action(action_id: str, request: Request) -> Response:
    guard(request)
    action = _store().get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    if action["status"] != "approved":
        raise HTTPException(status_code=409, detail="only approved actions can be exported")
    if action["kind"] == "calendar_event":
        try:
            start = datetime.fromisoformat(action["start_at"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            end = datetime.fromisoformat(action["end_at"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="calendar timestamps must include a timezone")
        lines = ("BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Athena//Reviewable Actions//EN", "BEGIN:VEVENT",
            f"UID:{action['id']}@athena", f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART:{start}", f"DTEND:{end}", f"SUMMARY:{_ics_escape(action['title'])}",
            f"DESCRIPTION:{_ics_escape(action['content'])}", "END:VEVENT", "END:VCALENDAR", "")
        body = "\r\n".join(_ics_fold(line) for line in lines)
        filename, media_type = f"{action_id}.ics", "text/calendar; charset=utf-8"
    else:
        body = f"# {action['title']}\n\n{action['content']}\n"
        if action["source"]:
            body += f"\nSource: {action['source']}\n"
        filename, media_type = f"{action_id}.md", "text/markdown; charset=utf-8"
    return Response(body, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
