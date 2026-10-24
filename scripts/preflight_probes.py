"""The three preflight probes and their single result type.

Each probe returns a ``ProbeResult`` — success or failure — so the reporter
never reads an ad-hoc dict key and a failure can never hide behind a KeyError.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena.config import llm  # noqa: E402
from athena.config.endpoint_guard import host_of  # noqa: E402
from athena.observability.litellm_usage import usage_scope  # noqa: E402
from athena.observability.redact import redact  # noqa: E402
from athena.observability.usage_hook import install_usage_logger  # noqa: E402

PROMPT = "Say OK"
MAX_TOKENS = 300


@dataclass
class ProbeResult:
    """Everything the report needs about one path, always populated."""

    path: str
    ok: bool = True
    error_type: str = ""
    error_message: str = ""
    host: str = ""
    reply_chars: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning_tokens: int = 0
    calls: int = 0

    @property
    def usage_missing(self) -> bool:
        """A crewai probe that produced text but reported no usage at all."""
        if not self.ok or not self.path.startswith("crewai"):
            return False
        return self.reply_chars > 0 and not (self.tokens_in or self.tokens_out or self.calls)

    @property
    def failed(self) -> bool:
        return not self.ok or self.usage_missing

    def status(self) -> str:
        """The status column: what happened, in as few words as honest."""
        if not self.ok:
            return f"FAILED {self.error_type or 'Error'}: {self.error_message[:120]}"
        prefix = f"ok ({self.reply_chars} chars)"
        if not self.usage_missing:
            return prefix
        suffix = " (server path)" if self.path == "crewai-server-style" else ""
        return f"{prefix} USAGE NOT CAPTURED{suffix}"


def failure(path: str, host: str, exc: BaseException) -> ProbeResult:
    """A failed probe. Messages are redacted and truncated; no keys survive."""
    return ProbeResult(
        path=path,
        ok=False,
        error_type=type(exc).__name__,
        error_message=(redact(str(exc)) or "")[:200],
        host=host,
    )


@dataclass
class PathInfo:
    """Where a path points, resolved without making a call."""

    name: str
    host: str
    model: str
    detail: str


def describe_paths() -> list[tuple[PathInfo, Callable[[], ProbeResult]]]:
    """Build every path's model object once, plus its live probe."""
    return [_adk_path(), _crew_path(), _structured_path(), _server_path()]


def _adk_path() -> tuple[PathInfo, Callable[[], ProbeResult]]:
    model = llm.get_adk_model("planner")
    info = PathInfo(
        "adk-lite-llm",
        host_of(model._additional_args.get("api_base") or ""),
        model.model,
        "api_base passed explicitly",
    )
    return info, lambda: probe_adk(info, model)


def _crew_path() -> tuple[PathInfo, Callable[[], ProbeResult]]:
    client = llm.get_crewai_llm("analyst")
    info = PathInfo(
        "crewai-plain",
        host_of(client.base_url or ""),
        client.model,
        "base_url + api_base passed explicitly",
    )
    return info, lambda: probe_crew(info, client, structured=False)


def _structured_path() -> tuple[PathInfo, Callable[[], ProbeResult]]:
    client = llm.get_crewai_llm("writer")
    env_base = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or ""
    info = PathInfo(
        "crewai-structured",
        host_of(env_base),
        client.model,
        "env OPENAI_BASE_URL (InternalInstructor passes no api_base)",
    )
    return info, lambda: probe_crew(info, client, structured=True)


def probe_adk(info: PathInfo, model: Any) -> ProbeResult:
    """One tiny call through the real LiteLlm wrapper."""
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    request = LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text=PROMPT)])],
        config=types.GenerateContentConfig(max_output_tokens=MAX_TOKENS),
    )

    async def run() -> ProbeResult:
        result = ProbeResult(path=info.name, host=info.host)
        text = 0
        async for response in model.generate_content_async(request):
            content = getattr(response, "content", None)
            parts = getattr(content, "parts", None) or []
            text += sum(len(part.text or "") for part in parts if not getattr(part, "thought", False))
            usage = getattr(response, "usage_metadata", None)
            if usage:
                result.tokens_in = getattr(usage, "prompt_token_count", 0) or result.tokens_in
                result.tokens_out = getattr(usage, "candidates_token_count", 0) or result.tokens_out
                result.reasoning_tokens = getattr(usage, "thoughts_token_count", 0) or result.reasoning_tokens
        result.reply_chars = text
        result.calls = 1
        return result

    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - the report is the point
        return failure(info.name, info.host, exc)


def probe_crew(info: PathInfo, client: Any, structured: bool) -> ProbeResult:
    """One tiny CrewAI call, with usage captured at the litellm layer."""
    install_usage_logger()
    result = ProbeResult(path=info.name, host=info.host)
    try:
        with usage_scope(info.name) as scope:
            if structured:
                from pydantic import BaseModel

                class Structured(BaseModel):
                    text: str

                raw = client.call(PROMPT, response_model=Structured)
            else:
                raw = client.call(PROMPT)
        # Usage is read after the call finishes, from the scope it accumulated into.
        result.reply_chars = len(str(raw))
        result.tokens_in = scope.tokens_in
        result.tokens_out = scope.tokens_out
        result.reasoning_tokens = scope.reasoning_tokens
        result.calls = scope.calls
    except Exception as exc:  # noqa: BLE001 - the report is the point
        return failure(info.name, info.host, exc)
    return result

def _server_path() -> tuple[PathInfo, Callable[[], ProbeResult]]:
    """The path the A2A server actually takes: crew_call in a worker thread."""
    info = PathInfo(
        "crewai-server-style",
        host_of(llm.api_base() or ""),
        llm._model_string(),
        "crew_call via asyncio.to_thread (what the server runs)",
    )
    return info, lambda: probe_server(info)


def probe_server(info: PathInfo) -> ProbeResult:
    """Run the real role code path, exactly as the server does."""
    from athena.agents.crew_roles import crew_call

    result = ProbeResult(path=info.name, host=info.host)

    try:
        from pydantic import BaseModel

        class Tiny(BaseModel):
            text: str

        async def run():
            return await asyncio.to_thread(
                crew_call, "analyst", "Reply with the single word OK.", PROMPT, Tiny
            )

        reply, stats = asyncio.run(run())
        result.reply_chars = len(str(reply))
        result.tokens_in = int(stats.get("tokens_in") or 0)
        result.tokens_out = int(stats.get("tokens_out") or 0)
        result.reasoning_tokens = int(stats.get("reasoning_tokens") or 0)
        result.calls = int(stats.get("calls") or 0)
    except Exception as exc:  # noqa: BLE001 - the report is the point
        return failure(info.name, info.host, exc)
    return result