"""The monitoring event: one row in the append-only event log.

Text fields are redacted and truncated by validators, so nothing that reaches
the store can carry a key or a whole document.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from athena.observability.redact import redact

EventKind = Literal[
    "stage_start",
    "stage_end",
    "model_call",
    "tool_call",
    "a2a_call",
    "blackboard_write",
    "limiter_wait",
    "error",
]

AgentName = Literal[
    "planner",
    "researcher",
    "rag_researcher",
    "web_researcher",
    "calc_researcher",
    "analyst",
    "writer",
    "critic",
    "supervisor",
]

Framework = Literal["adk", "crewai"]

MAX_TEXT = 300
MAX_EXTRA_CHARS = 2000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Event(BaseModel):
    """One monitoring event. ``seq`` is assigned by the store, not the caller."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str = Field(description="The blackboard run this event belongs to.")
    ts: str = Field(default_factory=utc_now, description="UTC ISO timestamp.")
    seq: int = Field(default=0, description="Monotonic per run; set by the store.")
    kind: EventKind = Field(description="What happened.")
    agent: str = Field(description="Which agent it happened to.")
    framework: Framework | str = Field(description="adk or crewai.")
    duration_ms: float | None = Field(default=None)
    tokens_in: int | None = Field(default=None)
    tokens_out: int | None = Field(default=None)
    ok: bool = Field(default=True)
    error_type: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    payload_summary: str | None = Field(default=None)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("error_message", "payload_summary")
    @classmethod
    def _clean_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return (redact(value) or "")[:MAX_TEXT]

    @field_validator("error_type")
    @classmethod
    def _clean_type(cls, value: str | None) -> str | None:
        return redact(value)[:80] if value else value

    @field_validator("extra")
    @classmethod
    def _small_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Keep ``extra`` small, JSON-safe and redacted."""
        def clean(node):
            if isinstance(node, str):
                return (redact(node) or "")[:MAX_TEXT]
            if isinstance(node, dict):
                return {str(key): clean(item) for key, item in list(node.items())[:20]}
            if isinstance(node, (list, tuple)):
                return [clean(item) for item in list(node)[:20]]
            return node if isinstance(node, (int, float, bool)) or node is None else str(node)

        return clean(value)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str) -> "Event":
        return cls.model_validate_json(payload)
