"""Saved prompt workflows for repeatable, user initiated chat tasks."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from athena.api.guards import guard
from athena.guardrails.secrets import contains_secret

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 prompt TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflows_updated ON workflows(updated_at DESC);
"""


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    path = Path(os.getenv("ATHENA_DATA_DIR") or "data") / "workflows.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class WorkflowIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    prompt: str = Field(min_length=1, max_length=8000)


class WorkflowPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    prompt: str | None = Field(default=None, min_length=1, max_length=8000)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@router.get("")
def list_workflows(request: Request) -> dict:
    guard(request)
    with _db() as conn:
        rows = conn.execute("SELECT * FROM workflows ORDER BY updated_at DESC, name COLLATE NOCASE").fetchall()
    return {"workflows": [dict(row) for row in rows]}


@router.post("", status_code=201)
def create_workflow(payload: WorkflowIn, request: Request) -> dict:
    guard(request)
    stamp = _now()
    item = {"id": uuid.uuid4().hex, **payload.model_dump(), "created_at": stamp, "updated_at": stamp}
    if any(contains_secret(item[key]) for key in ("name", "description", "prompt")):
        raise HTTPException(422, "Remove credentials from the workflow before saving")
    with _db() as conn:
        conn.execute(
            "INSERT INTO workflows(id,name,description,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            tuple(item[key] for key in ("id", "name", "description", "prompt", "created_at", "updated_at")),
        )
    return {"workflow": item}


@router.patch("/{workflow_id}")
def update_workflow(workflow_id: str, payload: WorkflowPatch, request: Request) -> dict:
    guard(request)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="provide at least one workflow field")
    if any(value is None for value in changes.values()):
        raise HTTPException(422, "Workflow fields cannot be null")
    if any(contains_secret(value) for value in changes.values()):
        raise HTTPException(422, "Remove credentials from the workflow before saving")
    with _db() as conn:
        if conn.execute("SELECT 1 FROM workflows WHERE id=?", (workflow_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        changes["updated_at"] = _now()
        assignments = ", ".join(f"{key}=?" for key in changes)
        conn.execute(f"UPDATE workflows SET {assignments} WHERE id=?", (*changes.values(), workflow_id))
        row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
    return {"workflow": dict(row)}


@router.delete("/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: str, request: Request) -> None:
    guard(request)
    with _db() as conn:
        cursor = conn.execute("DELETE FROM workflows WHERE id=?", (workflow_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="workflow not found")
