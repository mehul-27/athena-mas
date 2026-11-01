"""The chat tool catalogue and its bounded execution adapter.

The browser sends names only. A name is executable only when it exists in this
table and is currently available; everything else is reported back and never
reaches a handler. The handlers are the same functions the MAS MCP servers
expose, called in-process so chat does not spawn a subprocess per message.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

from athena.observability.redact import redact

MAX_OUTPUT_CHARS = 4000
MAX_SELECTION = 100


@dataclass(frozen=True)
class ToolSpec:
    name: str
    label: str
    description: str
    server: str
    parameters: dict[str, Any]
    readonly: bool = True


@dataclass(frozen=True)
class ToolSelection:
    allowed: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    reasons: tuple[tuple[str, str], ...] = ()

    def reason_map(self) -> dict[str, str]:
        return dict(self.reasons)


TOOL_SPECS: dict[str, ToolSpec] = {
    "rag_search": ToolSpec(
        name="rag_search",
        label="Document search",
        description=(
            "Search the indexed documents (the same corpus RAG uses) and return the most "
            "relevant passages with file name and page."
        ),
        server="athena-rag",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for in the documents."},
                "top_k": {"type": "integer", "description": "How many passages to return (1-20, default 5)."},
            },
            "required": ["query"],
        },
    ),
    "web_search": ToolSpec(
        name="web_search",
        label="Web search",
        description="Search the web through the configured provider and return ranked results (title, URL, snippet).",
        server="athena-web",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "max_results": {"type": "integer", "description": "How many results to return (1-20, default 5)."},
            },
            "required": ["query"],
        },
    ),
    "fetch_page": ToolSpec(
        name="fetch_page",
        label="Read a web page",
        description="Fetch one public web page by URL and return its readable text.",
        server="athena-web",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The absolute http(s) URL to fetch."},
                "max_chars": {"type": "integer", "description": "Maximum characters of text (1000-20000)."},
            },
            "required": ["url"],
        },
    ),
    "calculate": ToolSpec(
        name="calculate",
        label="Calculator",
        description="Evaluate an arithmetic expression safely (no variables, no code execution).",
        server="athena-calc",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "The arithmetic expression."}},
            "required": ["expression"],
        },
    ),
    "forecast": ToolSpec(
        name="forecast",
        label="Forecast",
        description="Forecast a numeric series with a linear trend or exponential smoothing.",
        server="athena-calc",
        parameters={
            "type": "object",
            "properties": {
                "values": {"type": "array", "items": {"type": "number"}, "description": "At least three values."},
                "horizon": {"type": "integer", "description": "How many steps to forecast (1-50)."},
            },
            "required": ["values", "horizon"],
        },
    ),
}


def clean_ids(values: Iterable[Any] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
        if len(result) >= MAX_SELECTION:
            break
    return result


def availability(name: str, target=None) -> tuple[bool, str]:
    if name == "rag_search":
        from athena.tools.rag.status import chunk_count

        if chunk_count() <= 0:
            return False, "No documents are indexed. Rebuild the index in Settings → Library."
        return True, ""
    if name == "web_search":
        provider = (getattr(target, "search_provider", "") or "").lower()
        if not provider:
            from athena.tools import settings as tool_settings

            provider = tool_settings.search_provider()
        if provider == "disabled":
            return False, "Web search is disabled (SEARCH_PROVIDER=disabled)."
        if provider not in {"duckduckgo", "searxng"}:
            return False, f"Unsupported search provider {provider!r}."
        return True, ""
    return True, ""


def resolve_selection(names: Iterable[Any] | None, target=None) -> ToolSelection:
    allowed: list[str] = []
    rejected: list[str] = []
    reasons: list[tuple[str, str]] = []
    for name in clean_ids(names):
        spec = TOOL_SPECS.get(name)
        if spec is None:
            rejected.append(name)
            reasons.append((name, "Unknown tool."))
            continue
        usable, reason = availability(name, target)
        if usable:
            allowed.append(name)
        else:
            rejected.append(name)
            reasons.append((name, reason))
    return ToolSelection(tuple(allowed), tuple(rejected), tuple(reasons))


def schemas(names: Iterable[str]) -> list[dict[str, Any]]:
    result = []
    for name in names:
        spec = TOOL_SPECS.get(name)
        if spec is None:
            continue
        result.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
        )
    return result


def catalog(target=None) -> list[dict[str, Any]]:
    rows = []
    for spec in TOOL_SPECS.values():
        usable, reason = availability(spec.name, target)
        rows.append(
            {
                "id": spec.name,
                "label": spec.label,
                "description": spec.description,
                "server": spec.server,
                "readonly": spec.readonly,
                "available": usable,
                "reason": reason,
                "parameters": spec.parameters,
            }
        )
    return rows


def _query(args: dict[str, Any]) -> str:
    value = str(args.get("query") or "").strip()
    if not value:
        raise ValueError("query is required")
    return value[:500]


def _integer(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(args.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _rag(args: dict[str, Any], _target) -> tuple[str, str, bool, str]:
    from athena.tools.rag.search import rag_search

    result = rag_search(_query(args), _integer(args, "top_k", 5, 1, 20))
    if not result.ok:
        return "", "Document search failed", False, result.error or "search failed"
    if not result.hits:
        return "No document passages matched.", "0 passages", True, ""
    lines = []
    for index, hit in enumerate(result.hits, 1):
        page = f", p{hit.page}" if hit.page else ""
        lines.append(f"[{index}] {hit.document}{page} (score {hit.score:.2f})\n{hit.text}")
    best = max(hit.score for hit in result.hits)
    return "\n\n".join(lines)[:MAX_OUTPUT_CHARS], f"{len(result.hits)} passages · best {best:.2f}", True, ""


def _web(args: dict[str, Any], target) -> tuple[str, str, bool, str]:
    from athena.tools.web.search import web_search

    result = web_search(
        _query(args),
        _integer(args, "max_results", 5, 1, 20),
        provider=getattr(target, "search_provider", None),
        endpoint=getattr(target, "search_endpoint", None),
    )
    if not result.ok:
        return "", "Web search failed", False, result.error or "search failed"
    if not result.results:
        return "No web results matched.", "0 results", True, ""
    lines = [
        f"[{index}] {hit.title} — {hit.url}\n{hit.snippet}"
        for index, hit in enumerate(result.results, 1)
    ]
    return "\n\n".join(lines)[:MAX_OUTPUT_CHARS], f"{len(result.results)} results", True, ""


def _fetch(args: dict[str, Any], _target) -> tuple[str, str, bool, str]:
    from athena.tools.web.fetch import fetch_page

    url = str(args.get("url") or "").strip()
    if not url:
        raise ValueError("url is required")
    result = fetch_page(url, max_chars=_integer(args, "max_chars", 6000, 1000, 20000))
    if result.status != "ok" or not result.text.strip():
        return "", "Page fetch failed", False, result.status or "fetch failed"
    text = f"{result.final_url or result.requested_url}\n{result.title}\n\n{result.text}"
    summary = f"{result.title or result.final_url} · {len(result.text)} chars"
    return text[:MAX_OUTPUT_CHARS], summary, True, ""


def _calc(args: dict[str, Any], _target) -> tuple[str, str, bool, str]:
    from athena.tools.calc import calculate

    expression = str(args.get("expression") or "").strip()
    if not expression:
        raise ValueError("expression is required")
    result = calculate(expression[:200])
    if not result.ok or result.value is None:
        return "", "Calculation failed", False, result.error or "invalid expression"
    return f"{expression} = {result.value}", f"= {result.value}", True, ""


def _forecast(args: dict[str, Any], _target) -> tuple[str, str, bool, str]:
    from athena.tools.forecast import forecast

    raw_values = args.get("values")
    if not isinstance(raw_values, list):
        raise ValueError("values must be an array")
    values = []
    for item in raw_values[:200]:
        try:
            values.append(float(item))
        except (TypeError, ValueError):
            raise ValueError("values must be numbers") from None
    if len(values) < 3:
        raise ValueError("at least three values are required")
    result = forecast(values, _integer(args, "horizon", 3, 1, 50))
    if not result.ok:
        return "", "Forecast failed", False, result.error or "forecast failed"
    payload = {
        "forecast": result.forecast,
        "method": result.method,
        "note": result.note,
    }
    text = json.dumps(payload, ensure_ascii=False)
    return text[:MAX_OUTPUT_CHARS], f"{len(result.forecast)} steps · {result.method}", True, ""


_HANDLERS = {
    "rag_search": _rag,
    "web_search": _web,
    "fetch_page": _fetch,
    "calculate": _calc,
    "forecast": _forecast,
}


def _run(name: str, args: dict[str, Any], target) -> tuple[str, str, bool, str]:
    spec = TOOL_SPECS[name]
    try:
        return _HANDLERS[name](args, target)
    except Exception as exc:  # noqa: BLE001 - a tool failure is a result
        return "", f"{spec.label} failed", False, f"{type(exc).__name__}: {exc}"


def _decode(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def execute(
    name: str,
    args: Any,
    target=None,
    timeout: float = 30.0,
) -> tuple[str, dict[str, Any]]:
    """Run one selected tool. Never raises; failures are normal events."""
    spec = TOOL_SPECS.get(name)
    parsed = _decode(args)
    started = time.perf_counter()
    if spec is None:
        return (
            f"{name or 'That tool'} is not available.",
            {
                "name": name or "(unknown)",
                "label": name or "Unknown tool",
                "status": "error",
                "summary": "Unknown tool",
                "elapsed_ms": 0.0,
                "error": "Unknown tool.",
            },
        )
    try:
        text, summary, ok, error = await asyncio.wait_for(
            asyncio.to_thread(_run, name, parsed, target), timeout=timeout
        )
    except asyncio.TimeoutError:
        text, summary, ok, error = "", f"{spec.label} timed out", False, f"timed out after {int(timeout)}s"
    except Exception as exc:  # noqa: BLE001 - execution must report, not raise
        text, summary, ok, error = "", f"{spec.label} failed", False, f"{type(exc).__name__}: {exc}"
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    safe_error = redact(str(error or ""))[:240] if error else ""
    event = {
        "name": name,
        "label": spec.label,
        "status": "ok" if ok else "error",
        "summary": (summary or "")[:240],
        "elapsed_ms": elapsed,
        "error": safe_error,
    }
    return text, event
