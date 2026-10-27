"""Result models for Athena's MCP tools.

Every tool returns one of these models and never raises: a failure is a normal
result with ``ok=False`` and a message, so a guardrail can inspect it later.
``tool_name`` and ``elapsed_ms`` are on every result so monitoring can attribute
a call without parsing the payload.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ToolResult(BaseModel):
    """Fields every tool result carries."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(description="Name of the MCP tool that produced this.")
    elapsed_ms: float = Field(description="Wall-clock duration of the call, in ms.")
    ok: bool = Field(default=True, description="False when the call failed.")
    error: str | None = Field(
        default=None, description="Human-readable error when ok is false."
    )


class RagHit(BaseModel):
    """One retrieved chunk of a corpus document."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(description="Stable, content-addressed chunk id.")
    document_id: str = Field(description="Id of the source document.")
    document: str = Field(description="Original file name of the source document.")
    chunk_index: int = Field(description="Position of the chunk within the document.")
    page: int | None = Field(
        default=None, description="Page the chunk starts on, when it can be verified."
    )
    text: str = Field(description="The retrieved chunk text.")
    score: float = Field(description="Hybrid similarity score, higher is closer.")


class RagResult(ToolResult):
    """Result of a RAG search."""

    tool_name: str = "rag_search"
    query: str = Field(description="The query that was searched for.")
    top_k: int = Field(description="Maximum number of hits requested.")
    hits: list[RagHit] = Field(
        default_factory=list, description="Hits, best first."
    )


class WebHit(BaseModel):
    """One web search result."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Page title as reported by the provider.")
    url: str = Field(description="Page URL.")
    snippet: str = Field(default="", description="Short excerpt from the page.")


class WebResult(ToolResult):
    """Result of a web search."""

    tool_name: str = "web_search"
    query: str = Field(description="The query that was searched for.")
    provider: str = Field(description="Search provider that produced the results.")
    max_results: int = Field(description="Maximum number of results requested.")
    results: list[WebHit] = Field(
        default_factory=list, description="Results, in provider order."
    )
    note: str | None = Field(
        default=None, description="Set when the search ran but found nothing."
    )


class CalcResult(ToolResult):
    """Result of evaluating an arithmetic expression."""

    tool_name: str = "calculate"
    expression: str = Field(description="The expression as submitted.")
    value: float | None = Field(
        default=None, description="The computed value when ok is true."
    )


class ForecastResult(ToolResult):
    """Result of forecasting a numeric series."""

    tool_name: str = "forecast"
    values: list[float] = Field(description="The input series.")
    horizon: int = Field(description="How many steps were forecast.")
    forecast: list[float] = Field(
        default_factory=list, description="Forecast values, one per step."
    )
    method: str = Field(
        default="", description="Method used: linear_trend or exponential_smoothing."
    )
    note: str = Field(default="", description="Short explanation of the choice and fit.")
