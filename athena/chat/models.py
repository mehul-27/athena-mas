"""Wire and execution types for interactive chat."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


class ChatMessageIn(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=8000)
    profile_id: str | None = Field(default=None, max_length=64)
    capabilities: list[str] = Field(default_factory=list, max_length=8)
    tools: list[str] = Field(default_factory=list, max_length=100)


class CreateConversationIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class RenameConversationIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class SetModelIn(BaseModel):
    profile_id: str = Field(default="", max_length=64)


class ChatConfigError(RuntimeError):
    """A chat turn cannot start; the route maps this to an HTTP status."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


class CapabilityError(ValueError):
    """An unknown capability id was requested."""


@dataclass(frozen=True)
class ChatTarget:
    """The resolved model call: everything LiteLLM needs, and no secrets in logs."""

    model: str
    api_key: str
    api_base: str
    reasoning_effort: str
    provider: str
    profile_id: str = ""
    profile_name: str = ""
    search_provider: str = "duckduckgo"
    search_endpoint: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "provider": self.provider,
            "model": self.model,
            "api_host": _host(self.api_base),
            "search_provider": self.search_provider,
        }


@dataclass
class LLMDelta:
    """One normalised streaming delta from a completion."""

    text: str = ""
    tool_index: int = -1
    tool_id: str = ""
    tool_name: str = ""
    tool_arguments: str = ""
    usage: dict[str, int] | None = None
    finish_reason: str = ""


@dataclass
class ToolEvent:
    id: str
    name: str
    label: str
    status: str
    round: int = 0
    summary: str = ""
    elapsed_ms: float = 0.0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            "status": self.status,
            "round": self.round,
            "summary": self.summary,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


@dataclass
class Grounded:
    context: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    refusal: str = ""


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url or "").hostname or ""
