"""Telemetry endpoints: cost, waterfall spans, client heatmaps and provenance.

Read-only, and derived **strictly** from what the run stored: the append-only
event log (``EVENTS_DB``) and the blackboard (``BLACKBOARD_DB``). Nothing here
calls a model, recomputes a pipeline value, or invents a number: when a datum was
never recorded the field is ``null`` (or the list empty) and the response says
why in ``missing``/``reason``.

Endpoints
---------
``GET /api/runs/{run_id}/telemetry``
    ``agents[]`` (start/end/duration, tokens, cost, tool calls, tool errors,
    errors, retries, handoffs, recoveries, limiter wait, A2A bytes, evidence
    contributions, terminal state), ``spans[]`` for a waterfall, ``totals``.
``GET /api/runs/{run_id}/heatmap.json``
    agent x metric and agent x time-bucket (stage-ordered) matrices with raw
    values, a normalisation hint, and the event ids behind every cell.
``GET /api/runs/{run_id}/cells?agent=&metric=|bucket=``
    the real events behind one cell.
``GET /api/runs/{run_id}/provenance?claim=|citation=``
    report citation -> evidence item -> source URL/doc page -> tool call ->
    agent -> run.

Cost is a list-price *estimate* from ``data/pricing.json`` (see
``athena.observability.cost``); an unrecorded model name or an unlisted model is
reported as ``unpriced``, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request

from athena.api.guards import guard
from athena.blackboard import Blackboard
from athena.observability.cost import CostEstimate, estimate_cost, pricing_meta
from athena.observability.events import Event
from athena.observability.redact import redact
from athena.observability.store import SQLiteEventStore

router = APIRouter(prefix="/api/runs", tags=["telemetry"])

RUN_SUMMARY_PREFIX = "run_summary"
#: Errors that the pipeline itself retries (config/reply_guard.py, crew_writer).
INFERRED_RETRY_ERRORS = ("empty_reply", "runaway_output")
SNIPPET_CHARS = 240


# --------------------------------------------------------------------------- io
def _store() -> SQLiteEventStore:
    return SQLiteEventStore()


def _events(run_id: str) -> list[Event]:
    try:
        return _store().events(run_id)
    except Exception:  # noqa: BLE001 - a broken store must not 500 a read view
        return []


def _blackboard(run_id: str) -> Blackboard:
    return Blackboard.open(run_id)


def _evidence(run_id: str) -> list[dict[str, Any]]:
    """The run's evidence items, or an empty list when none were written."""
    try:
        found = _blackboard(run_id).read("evidence")
    except Exception:  # noqa: BLE001 - no blackboard means no evidence to show
        return []
    if not found:
        return []
    items = found[0] if isinstance(found[0], list) else [found[0]]
    rows = []
    for item in items:
        rows.append({
            "id": str(getattr(item, "id", "")),
            "source_type": str(getattr(item, "source_type", "")),
            "source_ref": str(getattr(item, "source_ref", "")),
            "retrieved_by": str(getattr(item, "retrieved_by", "")),
            "provider": str(getattr(item, "provider", "") or ""),
            "score": getattr(item, "score", None),
            "content": redact(getattr(item, "content", "") or "")[:SNIPPET_CHARS * 4],
        })
    return rows


def _section(run_id: str, name: str) -> dict[str, Any] | None:
    try:
        found = _blackboard(run_id).read(name)
    except Exception:  # noqa: BLE001
        return None
    if not found:
        return None
    value = found[0]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------- time helpers
def _ts_ms(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp() * 1000.0
    except ValueError:
        return None


def _span_bounds(event: Event, start_ms: float) -> tuple[float | None, float | None]:
    """(start, end) offsets in ms for an event that carries a duration."""
    end = _ts_ms(event.ts)
    if end is None:
        return None, None
    duration = event.duration_ms
    if duration is None:
        return round(end - start_ms, 2), round(end - start_ms, 2)
    return round(end - start_ms - float(duration), 2), round(end - start_ms, 2)


def _run_window(events: Sequence[Event]) -> tuple[float, float]:
    stamps = [value for value in (_ts_ms(event.ts) for event in events) if value is not None]
    if not stamps:
        return 0.0, 0.0
    return min(stamps), max(stamps)


def _iso_of(start_ms: float, offset_ms: float | None) -> str | None:
    if offset_ms is None:
        return None
    try:
        return datetime.fromtimestamp((start_ms + offset_ms) / 1000.0).astimezone().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# --------------------------------------------------------------------- context
class Context:
    """Everything the shaping functions share, computed once per request."""

    def __init__(self, run_id: str, events: list[Event], evidence: list[dict]) -> None:
        self.run_id = run_id
        self.events = list(events)
        self.evidence = evidence
        self.start_ms, self.end_ms = _run_window(self.events)
        self.summary = _run_summary(self.events)
        self._costs: dict[str, CostEstimate] = {}
        self.evidence_by_agent: dict[str, list[dict]] = {}
        for item in evidence:
            self.evidence_by_agent.setdefault(item["retrieved_by"], []).append(item)
        self.evidence_write_events: dict[str, list[Event]] = {}
        for event in self.events:
            if event.kind == "blackboard_write" and (event.extra or {}).get("section") == "evidence":
                self.evidence_write_events.setdefault(event.agent, []).append(event)

    def cost_of(self, event: Event) -> CostEstimate | None:
        """The estimate for one ``model_call``, cached per event id."""
        if event.kind != "model_call":
            return None
        cached = self._costs.get(event.event_id)
        if cached is not None:
            return cached
        extra = event.extra or {}
        model = extra.get("model") or extra.get("model_name") or extra.get("model_id")
        estimate = estimate_cost(model, event.tokens_in, event.tokens_out)
        self._costs[event.event_id] = estimate
        return estimate

    def agent_events(self) -> dict[str, list[Event]]:
        grouped: dict[str, list[Event]] = {}
        for event in self.events:
            grouped.setdefault(event.agent, []).append(event)
        return grouped


def _run_summary(events: Sequence[Event]) -> dict[str, Any] | None:
    for event in reversed(list(events)):
        summary = event.extra or {}
        if event.payload_summary and event.payload_summary.startswith(RUN_SUMMARY_PREFIX):
            return dict(summary)
        if summary.get("pattern") and ("outcome" in summary or "stage_errors" in summary):
            return dict(summary)
    return None


# --------------------------------------------------------------------- metrics
@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    unit: str


METRICS: tuple[Metric, ...] = (
    Metric("stage_ms", "stage ms", "ms"),
    Metric("model_calls", "model calls", "count"),
    Metric("tokens_in", "tokens in", "tokens"),
    Metric("tokens_out", "tokens out", "tokens"),
    Metric("reasoning_tokens", "reasoning", "tokens"),
    Metric("tool_calls", "tool calls", "count"),
    Metric("tool_errors", "tool errors", "count"),
    Metric("errors", "errors", "count"),
    Metric("retries", "retries", "count"),
    Metric("retries_inferred", "retries (inferred)", "count"),
    Metric("guardrails", "guardrails", "count"),
    Metric("handoffs", "handoffs", "count"),
    Metric("recoveries", "recoveries", "count"),
    Metric("limiter_wait_ms", "limiter wait ms", "ms"),
    Metric("a2a_bytes", "a2a bytes", "bytes"),
    Metric("evidence_items", "evidence", "count"),
    Metric("cost_usd", "cost (USD)", "usd"),
)

METRIC_BY_NAME = {metric.name: metric for metric in METRICS}
METRIC_NAMES = tuple(metric.name for metric in METRICS)


def _is_run_summary_event(event: Event) -> bool:
    return bool(event.payload_summary and event.payload_summary.startswith(RUN_SUMMARY_PREFIX))


def _is_handoff(event: Event) -> bool:
    if event.kind == "handoff":
        return True
    return event.kind == "a2a_call" and bool((event.extra or {}).get("handoff"))


def _a2a_bytes(event: Event) -> int:
    extra = event.extra or {}
    try:
        return int(extra.get("request_bytes") or 0) + int(extra.get("response_bytes") or 0)
    except (TypeError, ValueError):
        return 0


def _contribution(ctx: Context, event: Event, metric: str) -> float | None:
    """This event's contribution to ``metric``; None means "not priced/metric-less"."""
    extra = event.extra or {}
    if metric == "stage_ms":
        return None if event.kind != "stage_end" else float(event.duration_ms or 0.0)
    if metric == "model_calls":
        return None if event.kind != "model_call" else 1.0
    if metric == "tokens_in":
        return None if event.kind != "model_call" else float(event.tokens_in or 0)
    if metric == "tokens_out":
        return None if event.kind != "model_call" else float(event.tokens_out or 0)
    if metric == "reasoning_tokens":
        return None if event.kind != "model_call" else float(extra.get("reasoning_tokens") or 0)
    if metric == "tool_calls":
        return None if event.kind != "tool_call" else 1.0
    if metric == "tool_errors":
        return None if event.kind != "tool_call" or event.ok else 1.0
    if metric == "errors":
        return None if event.kind != "error" else 1.0
    if metric == "retries":
        return None if event.kind != "retry" else 1.0
    if metric == "retries_inferred":
        if event.kind != "error" or event.error_type not in INFERRED_RETRY_ERRORS:
            return None
        return 1.0
    if metric == "guardrails":
        return None if event.kind != "guardrail" else 1.0
    if metric == "handoffs":
        return 1.0 if _is_handoff(event) else None
    if metric == "recoveries":
        return None if event.kind != "recovery" else 1.0
    if metric == "limiter_wait_ms":
        return None if event.kind != "limiter_wait" else float(event.duration_ms or 0.0)
    if metric == "a2a_bytes":
        return None if event.kind != "a2a_call" else float(_a2a_bytes(event))
    if metric == "evidence_items":
        if event.kind == "blackboard_write" and extra.get("section") == "evidence":
            return 1.0
        return None
    if metric == "cost_usd":
        estimate = ctx.cost_of(event)
        if estimate is None:
            return None
        return estimate.cost_usd
    return None


def _metric_events(ctx: Context, agent: str, metric: str) -> list[Event]:
    if metric == "cost_usd":
        # Every model call is behind the cost column, priced or not: an unpriced
        # call is exactly what the drill-down must be able to show.
        return [
            event for event in ctx.events
            if event.agent == agent and event.kind == "model_call"
        ]
    return [
        event for event in ctx.events
        if event.agent == agent and _contribution(ctx, event, metric) is not None
    ]


def _metric_value(ctx: Context, agent: str, metric: str) -> float | None:
    """Sum of the agent's contributions; ``None`` only when nothing contributed."""
    if metric == "evidence_items":
        items = ctx.evidence_by_agent.get(agent) or []
        if items:
            return float(len(items))
        return float(len(_metric_events(ctx, agent, metric))) or None
    if metric == "cost_usd":
        total = None
        for event in _metric_events(ctx, agent, metric):
            value = _contribution(ctx, event, metric)
            if value is None:
                continue
            total = (total or 0.0) + float(value)
        return None if total is None else round(total, 9)
    values = [
        float(value) for value in (
            _contribution(ctx, event, metric) for event in ctx.events if event.agent == agent
        ) if value is not None
    ]
    if not values:
        return None
    return round(sum(values), 6)


def _agent_status(events: Sequence[Event], run_finished: bool) -> str:
    """A per-agent terminal state derived only from its own recorded events."""
    if not events:
        return "no_events"
    last_stage_end = next((event for event in reversed(list(events)) if event.kind == "stage_end"), None)
    if last_stage_end is not None:
        if last_stage_end.ok is False:
            return "failed"
        return "succeeded"
    if any(event.kind == "error" and event.ok is False for event in events):
        return "failed"
    return "incomplete" if run_finished else "running"


def _run_terminal_state(ctx: Context) -> str:
    summary = ctx.summary or {}
    if summary.get("failed"):
        return "failed"
    outcome = summary.get("outcome")
    if outcome:
        return str(outcome)
    if not ctx.events:
        return "no_events"
    if any(_is_run_summary_event(event) for event in ctx.events):
        return "incomplete"
    return "running"


def _evidence_contributions(ctx: Context, agent: str) -> dict[str, Any]:
    items = ctx.evidence_by_agent.get(agent) or []
    writes = ctx.evidence_write_events.get(agent) or []
    if items:
        return {
            "count": len(items),
            "source": "blackboard",
            "evidence_ids": [item["id"] for item in items],
            "event_ids": [event.event_id for event in writes],
            "reason": "" if writes else "evidence came from the blackboard; no "
                                       "blackboard_write event was recorded for it",
        }
    if writes:
        return {
            "count": len(writes),
            "source": "events",
            "evidence_ids": [],
            "event_ids": [event.event_id for event in writes],
            "reason": "no evidence section on the blackboard; counted from events",
        }
    return {"count": 0, "source": "none", "evidence_ids": [], "event_ids": [],
            "reason": "this agent contributed no evidence"}


def _agent_cost(ctx: Context, events: Sequence[Event]) -> dict[str, Any]:
    estimates = [
        estimate for estimate in (ctx.cost_of(event) for event in events) if estimate is not None
    ]
    priced = [estimate.cost_usd for estimate in estimates if estimate.cost_usd is not None]
    unpriced = [estimate for estimate in estimates if estimate.cost_usd is None]
    if not estimates:
        return {"cost_usd": None, "cost_basis": "no_model_calls", "priced_calls": 0,
                "unpriced_calls": 0, "models": [], "reason": "this agent made no model call"}
    basis = "list_price"
    if unpriced and not priced:
        basis = "unpriced"
    elif unpriced and priced:
        basis = "partly_unpriced"
    return {
        "cost_usd": round(sum(priced), 9) if priced else None,
        "cost_basis": basis,
        "priced_calls": len(priced),
        "unpriced_calls": len(unpriced),
        "models": sorted({
            str(estimate.model) for estimate in estimates if estimate.model
        }),
        "reason": "" if not unpriced else "; ".join(
            sorted({estimate.reason for estimate in unpriced if estimate.reason})
        ),
    }


def _tokens(ctx: Context, events: Sequence[Event]) -> dict[str, Any]:
    """Tokens from model_call events, falling back to the A2A ack (like metrics.py)."""
    model_calls = [event for event in events if event.kind == "model_call"]
    tokens_in = sum(int(event.tokens_in or 0) for event in model_calls)
    tokens_out = sum(int(event.tokens_out or 0) for event in model_calls)
    reasoning = sum(int((event.extra or {}).get("reasoning_tokens") or 0) for event in model_calls)
    source = "model_call"
    if not tokens_in and not tokens_out:
        a2a = [event for event in events if event.kind == "a2a_call"]
        a2a_in = sum(int(event.tokens_in or 0) for event in a2a)
        a2a_out = sum(int(event.tokens_out or 0) for event in a2a)
        if a2a_in or a2a_out:
            tokens_in, tokens_out = a2a_in, a2a_out
            reasoning = sum(int((event.extra or {}).get("reasoning_tokens") or 0) for event in a2a)
            source = "a2a_ack"
    return {"tokens_in": tokens_in, "tokens_out": tokens_out,
            "reasoning_tokens": reasoning, "tokens_source": source}


def _agent_rows(ctx: Context) -> list[dict[str, Any]]:
    grouped = ctx.agent_events()
    finished = bool(_run_summary(ctx.events))
    rows: list[dict[str, Any]] = []
    for agent in sorted(grouped):
        events = grouped[agent]
        stamps = [value for value in (_ts_ms(event.ts) for event in events) if value is not None]
        start_offset = round(min(stamps) - ctx.start_ms, 2) if stamps else None
        end_offset = round(max(stamps) - ctx.start_ms, 2) if stamps else None
        stage_ms = _metric_value(ctx, agent, "stage_ms")
        duration = stage_ms if stage_ms is not None else (
            round(end_offset - start_offset, 2)
            if start_offset is not None and end_offset is not None else None
        )
        tokens = _tokens(ctx, events)
        cost = _agent_cost(ctx, events)
        metrics = {name: _metric_value(ctx, agent, name) for name in METRIC_NAMES}
        rows.append({
            "agent": agent,
            "framework": events[0].framework or "",
            "start_ms": start_offset,
            "end_ms": end_offset,
            "start_ts": _iso_of(ctx.start_ms, start_offset),
            "end_ts": _iso_of(ctx.start_ms, end_offset),
            "duration_ms": duration,
            "duration_basis": "sum of stage_end" if stage_ms is not None
                              else "first to last recorded event",
            **tokens,
            "cost_usd": cost["cost_usd"],
            "cost_basis": cost["cost_basis"],
            "models": cost["models"],
            "cost_reason": cost["reason"],
            "priced_calls": cost["priced_calls"],
            "unpriced_calls": cost["unpriced_calls"],
            "model_calls": int(metrics.get("model_calls") or 0),
            "tool_calls": int(metrics.get("tool_calls") or 0),
            "tool_errors": int(metrics.get("tool_errors") or 0),
            "errors": int(metrics.get("errors") or 0),
            "retries": int(metrics.get("retries") or 0),
            "retries_inferred": int(metrics.get("retries_inferred") or 0),
            "guardrails": int(metrics.get("guardrails") or 0),
            "handoffs": int(metrics.get("handoffs") or 0),
            "recoveries": int(metrics.get("recoveries") or 0),
            "limiter_wait_ms": float(metrics.get("limiter_wait_ms") or 0.0),
            "a2a_bytes": int(metrics.get("a2a_bytes") or 0),
            "evidence_contributions": _evidence_contributions(ctx, agent),
            "terminal_state": _agent_status(events, finished),
            "event_ids": [event.event_id for event in events],
            "event_count": len(events),
            "metrics": metrics,
        })
    return rows


# --------------------------------------------------------------------- spans
def _spans(ctx: Context) -> list[dict[str, Any]]:
    """Waterfall spans: real events, geometry derived from ts and duration_ms."""
    open_starts: dict[tuple[str, str], list[Event]] = {}
    spans: list[dict[str, Any]] = []
    for event in ctx.events:
        scope = str((event.extra or {}).get("stage_scope") or "")
        if event.kind == "stage_start":
            open_starts.setdefault((event.agent, scope), []).append(event)
            continue
        if event.kind == "stage_end" and not _is_run_summary_event(event):
            pending = open_starts.get((event.agent, scope)) or []
            started = pending.pop(0) if pending else None
            if started is None:
                # A start that recorded a different (or no) scope is still the
                # best start we have; prefer it over recomputing from ts-duration.
                for key in list(open_starts):
                    if key[0] == event.agent and open_starts[key]:
                        started = open_starts[key].pop(0)
                        break
            start_offset = _ts_ms(started.ts) - ctx.start_ms if started is not None else None
            end_offset = _ts_ms(event.ts)
            end_offset = end_offset - ctx.start_ms if end_offset is not None else None
            if start_offset is None:
                start_offset, end_offset = _span_bounds(event, ctx.start_ms)
            spans.append({
                "span_id": event.event_id,
                "event_id": event.event_id,
                "start_event_id": started.event_id if started is not None else None,
                "agent": event.agent,
                "framework": event.framework or "",
                "kind": "stage",
                "label": f"{event.agent} stage",
                "seq": event.seq,
                "start_offset_ms": None if start_offset is None else round(start_offset, 2),
                "end_offset_ms": end_offset,
                "duration_ms": event.duration_ms,
                "ok": event.ok,
                "stage_scope": scope,
                "open": False,
            })
            continue
        if event.kind == "model_call" and event.duration_ms is None:
            continue
        if event.duration_ms is None:
            continue
        start_offset, end_offset = _span_bounds(event, ctx.start_ms)
        label = event.payload_summary or event.kind
        if event.kind == "tool_call":
            label = f"tool: {(event.extra or {}).get('tool') or event.payload_summary or 'tool_call'}"
        elif event.kind == "a2a_call":
            label = f"a2a: {(event.extra or {}).get('role') or event.agent}"
        spans.append({
            "span_id": event.event_id,
            "event_id": event.event_id,
            "start_event_id": None,
            "agent": event.agent,
            "framework": event.framework or "",
            "kind": event.kind,
            "label": str(label)[:120],
            "seq": event.seq,
            "start_offset_ms": start_offset,
            "end_offset_ms": end_offset,
            "duration_ms": event.duration_ms,
            "ok": event.ok,
            "stage_scope": scope,
            "open": False,
        })

    for (agent, scope), pending in open_starts.items():
        for started in pending:
            offset = _ts_ms(started.ts)
            offset = round(offset - ctx.start_ms, 2) if offset is not None else None
            spans.append({
                "span_id": started.event_id,
                "event_id": started.event_id,
                "start_event_id": None,
                "agent": agent,
                "framework": started.framework or "",
                "kind": "stage",
                "label": f"{agent} stage (open)",
                "seq": started.seq,
                "start_offset_ms": offset,
                "end_offset_ms": None,
                "duration_ms": None,
                "ok": started.ok,
                "stage_scope": scope,
                "open": True,
            })

    spans.sort(key=lambda span: (span["start_offset_ms"] is None,
                                 span["start_offset_ms"] or 0.0, span["seq"]))
    return spans


# --------------------------------------------------------------------- totals
def _totals(ctx: Context, rows: list[dict[str, Any]]) -> dict[str, Any]:
    estimates = [ctx.cost_of(event) for event in ctx.events if event.kind == "model_call"]
    estimates = [estimate for estimate in estimates if estimate is not None]
    priced = [estimate.cost_usd for estimate in estimates if estimate.cost_usd is not None]
    unpriced = [estimate for estimate in estimates if estimate.cost_usd is None]
    basis = "list_price"
    if estimates and not priced:
        basis = "unpriced"
    elif unpriced:
        basis = "partly_unpriced"
    elif not estimates:
        basis = "no_model_calls"
    return {
        "run_id": ctx.run_id,
        "terminal_state": _run_terminal_state(ctx),
        "wall_ms": round(ctx.end_ms - ctx.start_ms, 2) if ctx.events else None,
        "events": len(ctx.events),
        "agents": len(rows),
        "tokens_in": sum(int(row["tokens_in"] or 0) for row in rows),
        "tokens_out": sum(int(row["tokens_out"] or 0) for row in rows),
        "reasoning_tokens": sum(int(row["reasoning_tokens"] or 0) for row in rows),
        "model_calls": sum(int(row["model_calls"] or 0) for row in rows),
        "tool_calls": sum(int(row["tool_calls"] or 0) for row in rows),
        "tool_errors": sum(int(row["tool_errors"] or 0) for row in rows),
        "errors": sum(int(row["errors"] or 0) for row in rows),
        "retries": sum(int(row["retries"] or 0) for row in rows),
        "retries_inferred": sum(int(row["retries_inferred"] or 0) for row in rows),
        "guardrails": sum(int(row["guardrails"] or 0) for row in rows),
        "handoffs": sum(int(row["handoffs"] or 0) for row in rows),
        "recoveries": sum(int(row["recoveries"] or 0) for row in rows),
        "limiter_wait_ms": round(sum(float(row["limiter_wait_ms"] or 0) for row in rows), 2),
        "a2a_bytes": sum(int(row["a2a_bytes"] or 0) for row in rows),
        "evidence_items": sum(int(row["evidence_contributions"]["count"]) for row in rows),
        "cost_usd": round(sum(priced), 9) if priced else None,
        "cost_basis": basis,
        "priced_calls": len(priced),
        "unpriced_calls": len(unpriced),
        "unpriced_models": sorted({str(estimate.model) for estimate in unpriced if estimate.model}),
        "cost_reason": "" if not unpriced else (
            "some model calls have no model name or no list price; they are excluded "
            "from the total and counted in unpriced_calls"
        ),
    }


def _missing(ctx: Context, rows: list[dict]) -> list[str]:
    missing: list[str] = []
    if not ctx.events:
        missing.append("no events are stored for this run")
    if not rows and ctx.events:
        missing.append("events exist but none name an agent")
    if not ctx.evidence:
        missing.append("no evidence section on the blackboard")
    if not (ctx.summary or {}).get("outcome"):
        missing.append("no run summary event, so the run-level outcome is unknown")
    if ctx.events and not any(event.kind == "model_call" for event in ctx.events):
        missing.append("no model_call events, so tokens and cost cannot be priced")
    return missing


# ------------------------------------------------------------------- endpoints
@router.get("/{run_id}/telemetry")
def telemetry(run_id: str, request: Request) -> dict[str, Any]:
    guard(request)
    events = _events(run_id)
    ctx = Context(run_id, events, _evidence(run_id))
    rows = _agent_rows(ctx)
    return {
        "run_id": run_id,
        "exists": bool(events),
        "pattern": (ctx.summary or {}).get("pattern"),
        "start_ts": _iso_of(ctx.start_ms, 0) if events else None,
        "end_ts": _iso_of(ctx.start_ms, round(ctx.end_ms - ctx.start_ms, 2)) if events else None,
        "agents": rows,
        "spans": _spans(ctx),
        "totals": _totals(ctx, rows),
        "cost_basis": pricing_meta(),
        "missing": _missing(ctx, rows),
        "reason": "" if events else f"no events are stored for run {run_id}",
        "notes": [
            "every value is derived from the stored event log and blackboard",
            "retries counts retry events; retries_inferred counts empty_reply/"
            "runaway_output errors, which the pipeline retries without a retry event",
            "handoffs counts handoff events and a2a_call events carrying extra.handoff",
            "cost is a list-price estimate; unpriced calls are excluded from the total",
        ],
    }


def _normalise(values: list[float | None]) -> tuple[list[float | None], dict[str, Any]]:
    known = [value for value in values if value is not None]
    if not known:
        return [None for _ in values], {"min": None, "max": None, "basis": "no values"}
    low, high = min(known), max(known)
    if high <= low:
        level = 0.0 if high == 0 else 0.5
        return [None if value is None else level for value in values], {
            "min": low, "max": high, "basis": "all values equal",
        }
    return [
        None if value is None else round((value - low) / (high - low), 4)
        for value in values
    ], {"min": low, "max": high, "basis": "min-max within the column"}


def _buckets(ctx: Context) -> list[dict[str, Any]]:
    """One bucket per completed stage, in stage order (the real event order)."""
    buckets = []
    for event in ctx.events:
        if event.kind != "stage_end" or _is_run_summary_event(event):
            continue
        start_offset, end_offset = _span_bounds(event, ctx.start_ms)
        index = len(buckets)
        buckets.append({
            "index": index,
            "label": f"{index + 1}. {event.agent}",
            "agent": event.agent,
            "event_id": event.event_id,
            "seq": event.seq,
            "start_offset_ms": start_offset,
            "end_offset_ms": end_offset,
            "duration_ms": event.duration_ms,
            "stage_scope": str((event.extra or {}).get("stage_scope") or ""),
        })
    return buckets


def _overlap_ms(span: dict[str, Any], bucket: dict[str, Any]) -> float:
    start, end = span["start_offset_ms"], span["end_offset_ms"]
    if start is None or end is None:
        return 0.0
    low, high = bucket["start_offset_ms"], bucket["end_offset_ms"]
    if low is None or high is None:
        return 0.0
    return max(0.0, min(end, high) - max(start, low))


@router.get("/{run_id}/heatmap.json")
def heatmap_json(run_id: str, request: Request) -> dict[str, Any]:
    guard(request)
    events = _events(run_id)
    ctx = Context(run_id, events, _evidence(run_id))
    rows = _agent_rows(ctx)
    agents = [row["agent"] for row in rows]

    metric_matrix: dict[str, dict[str, dict[str, Any]]] = {}
    ranges: dict[str, dict[str, Any]] = {}
    for metric in METRICS:
        raw = [row["metrics"].get(metric.name) for row in rows]
        normalised, hint = _normalise(raw)
        ranges[metric.name] = hint
        for position, row in enumerate(rows):
            cell = {
                "metric": metric.name,
                "label": metric.label,
                "unit": metric.unit,
                "raw": None if raw[position] is None else round(float(raw[position]), 6),
                "normalised": normalised[position],
                "event_ids": [event.event_id for event in _metric_events(ctx, row["agent"], metric.name)],
            }
            if metric.name == "evidence_items":
                cell["event_ids"] = list(row["evidence_contributions"]["event_ids"])
            metric_matrix.setdefault(row["agent"], {})[metric.name] = cell

    buckets = _buckets(ctx)
    spans_by_agent: dict[str, list[dict[str, Any]]] = {}
    for span in _spans(ctx):
        if span["kind"] == "stage" and not span["open"]:
            continue
        spans_by_agent.setdefault(span["agent"], []).append(span)

    bucket_matrix: dict[str, dict[str, dict[str, Any]]] = {}
    bucket_ranges: list[dict[str, Any]] = []
    for bucket in buckets:
        raw_row: list[float | None] = []
        cell_event_ids: list[list[str]] = []
        for agent in agents:
            overlaps = []
            ids = []
            for span in spans_by_agent.get(agent, []):
                overlap = _overlap_ms(span, bucket)
                if overlap > 0:
                    overlaps.append(overlap)
                    ids.append(span["event_id"])
            raw_row.append(round(sum(overlaps), 2) if overlaps else 0.0)
            cell_event_ids.append(ids)
        normalised, hint = _normalise(raw_row)
        bucket_ranges.append({"bucket": bucket["index"], **hint})
        for position, agent in enumerate(agents):
            ids = cell_event_ids[position]
            if agent == bucket["agent"] and bucket["event_id"] not in ids:
                # The bucket's own stage record is the floor of the owner's cell:
                # /cells returns it too, so the two views stay consistent.
                ids = [bucket["event_id"], *ids]
            bucket_matrix.setdefault(agent, {})[str(bucket["index"])] = {
                "bucket": bucket["index"],
                "label": bucket["label"],
                "unit": "ms",
                "raw": raw_row[position],
                "normalised": normalised[position],
                "event_ids": ids,
            }

    return {
        "run_id": run_id,
        "exists": bool(events),
        "agents": agents,
        "metrics": [
            {"name": metric.name, "label": metric.label, "unit": metric.unit}
            for metric in METRICS
        ],
        "metric_matrix": metric_matrix,
        "metric_normalisation": {
            "basis": "min-max per metric column, across the agents of this run",
            "ranges": ranges,
        },
        "buckets": buckets,
        "bucket_matrix": bucket_matrix,
        "bucket_normalisation": {
            "basis": "min-max per stage column, across the agents of this run",
            "buckets": bucket_ranges,
        },
        "bucket_events_basis": (
            "a bucket's value is the overlap in ms of the agent's own non-stage spans "
            "with the stage window (stage spans are excluded so a stage is not counted "
            "twice: its duration is the bucket width); the owner's cell also lists the "
            "bucket's own stage_end event id, which is the same record /cells returns first"
        ),
        "png_fallback": f"/api/runs/{run_id}/heatmap.png",
        "cost_basis": pricing_meta(),
        "reason": "" if events else f"no events are stored for run {run_id}",
        "missing": _missing(ctx, rows),
    }


@router.get("/{run_id}/cells")
def cells(
    run_id: str,
    request: Request,
    agent: str = Query(default="", description="agent row to drill into"),
    metric: str = Query(default="", description="metric column (see /heatmap.json)"),
    bucket: str = Query(default="", description="time-bucket index from /heatmap.json"),
) -> dict[str, Any]:
    """The real events behind one heatmap cell."""
    guard(request)
    if not metric and not bucket:
        raise HTTPException(status_code=422, detail="pass metric= or bucket=")
    if metric and metric not in METRIC_BY_NAME:
        raise HTTPException(
            status_code=422,
            detail=f"unknown metric {metric!r}; known: {', '.join(METRIC_NAMES)}",
        )
    events = _events(run_id)
    ctx = Context(run_id, events, _evidence(run_id))
    if not agent:
        raise HTTPException(status_code=422, detail="pass agent=")

    payload: list[dict[str, Any]] = []
    reason = ""
    if metric:
        selected = _metric_events(ctx, agent, metric)
        payload = [_event_json(event) for event in selected]
        if metric == "evidence_items":
            # The value of this column comes from the blackboard, so the cell
            # lists the evidence items themselves next to the write events.
            payload = payload + [
                {
                    "kind": "evidence_item",
                    "id": item["id"],
                    "source_ref": item["source_ref"],
                    "retrieved_by": item["retrieved_by"],
                    "score": item["score"],
                    "provider": item["provider"],
                    "snippet": (item.get("content") or "")[:SNIPPET_CHARS],
                }
                for item in (ctx.evidence_by_agent.get(agent) or [])
            ]
        if not payload:
            reason = (
                f"no stored event of agent {agent!r} contributes to {metric!r}"
                if events else f"no events are stored for run {run_id}"
            )
    else:
        try:
            index = int(bucket)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="bucket= must be an integer") from None
        buckets = _buckets(ctx)
        if index < 0 or index >= len(buckets):
            return {
                "run_id": run_id, "agent": agent, "bucket": index, "metric": None,
                "count": 0, "events": [],
                "reason": f"this run has {len(buckets)} stage bucket(s); {index} does not exist",
            }
        target = buckets[index]
        spans = [span for span in _spans(ctx) if span["agent"] == agent]
        ids = {
            span["event_id"] for span in spans
            if span["kind"] != "stage" and _overlap_ms(span, target) > 0
        }
        payload = [_event_json(event) for event in ctx.events if event.event_id in ids]
        if agent == target["agent"]:
            # The bucket's own stage record is the floor of that agent's cell.
            payload = [_event_json(event) for event in ctx.events
                       if event.event_id == target["event_id"]] + payload
        if not payload:
            reason = (
                f"agent {agent!r} recorded no span inside bucket {index} "
                f"({target['label']})"
            )

    return {
        "run_id": run_id,
        "agent": agent,
        "metric": metric or None,
        "bucket": None if metric else int(bucket),
        "count": len(payload),
        "events": payload,
        "reason": reason,
        "notes": [
            "events are returned as stored (already redacted at write time)",
            "an evidence_items cell also lists the blackboard evidence items",
        ],
    }


def _event_json(event: Event) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "seq": event.seq,
        "ts": event.ts,
        "kind": event.kind,
        "agent": event.agent,
        "framework": event.framework,
        "duration_ms": event.duration_ms,
        "tokens_in": event.tokens_in,
        "tokens_out": event.tokens_out,
        "ok": event.ok,
        "error_type": event.error_type,
        "error_message": event.error_message,
        "payload_summary": event.payload_summary,
        "extra": event.extra or {},
    }


# ----------------------------------------------------------------- provenance
def _normalised_ref(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.rstrip("/")
        return f"{host}{path}"
    return text.rstrip("/")


def _ref_match(left: str, right: str) -> str | None:
    """How two source references match: exact, normalised, or same document."""
    if not left or not right:
        return None
    if left.strip() == right.strip():
        return "exact"
    if _normalised_ref(left) and _normalised_ref(left) == _normalised_ref(right):
        return "normalised"
    left_doc = left.split("#")[0].strip().rstrip("/")
    right_doc = right.split("#")[0].strip().rstrip("/")
    if left_doc and left_doc == right_doc:
        return "document"
    return None


def _tool_calls(ctx: Context) -> list[Event]:
    return [event for event in ctx.events if event.kind == "tool_call"]


def _call_refs(event: Event) -> list[str]:
    extra = event.extra or {}
    refs = [str(extra.get("requested_url") or ""), str(extra.get("final_url") or "")]
    for source in extra.get("sources") or []:
        if isinstance(source, dict):
            refs.append(str(source.get("url") or ""))
            document = str(source.get("document") or "")
            page = source.get("page")
            if document and page:
                refs.append(f"{document}#p{page}")
            elif document:
                refs.append(document)
    return [ref for ref in refs if ref]


def _match_tool_call(ctx: Context, source_ref: str, agent: str) -> tuple[Event | None, str]:
    best: tuple[Event | None, str] = (None, "")
    for event in _tool_calls(ctx):
        for ref in _call_refs(event):
            match = _ref_match(ref, source_ref)
            if match:
                if event.agent == agent:
                    return event, match
                if best[0] is None:
                    best = (event, match + "_other_agent")
    if best[0] is not None:
        return best
    # Last resort, clearly labelled: the closest tool call the same agent made.
    same_agent = [event for event in _tool_calls(ctx) if event.agent == agent]
    if same_agent:
        return same_agent[-1], "unmatched_same_agent_latest"
    return None, ""


def _citation_index(report: dict[str, Any] | None, value: str) -> int | None:
    citations = (report or {}).get("citations") or []
    if value.isdigit():
        index = int(value)
        if 1 <= index <= len(citations):
            return index - 1
        return None
    for position, citation in enumerate(citations):
        if _ref_match(str(citation.get("source_ref") or ""), value):
            return position
    return None


def _provenance_chain(
    ctx: Context,
    citation: dict[str, Any] | None,
    evidence_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    missing: list[str] = []
    chain: list[dict[str, Any]] = []
    if citation is not None:
        chain.append({
            "hop": "citation",
            "source_type": citation.get("source_type"),
            "source_ref": citation.get("source_ref"),
            "quote": citation.get("quote_or_summary"),
        })
    if not evidence_items:
        missing.append("no blackboard evidence item matches this source reference")

    for item in evidence_items:
        chain.append({
            "hop": "evidence",
            "evidence_id": item["id"],
            "source_type": item["source_type"],
            "source_ref": item["source_ref"],
            "retrieved_by": item["retrieved_by"],
            "provider": item["provider"],
            "match": item.get("match"),
            "snippet": (item.get("content") or "")[:SNIPPET_CHARS],
        })
        parsed = urlparse(item["source_ref"] if "://" in item["source_ref"] else "")
        document = item["source_ref"].split("#")[0]
        page = ""
        if "#p" in item["source_ref"]:
            page = item["source_ref"].split("#p")[-1].split("-")[0]
        chain.append({
            "hop": "source",
            "url": item["source_ref"] if parsed.scheme else "",
            "host": (parsed.hostname or "") if parsed.scheme else "",
            "document": document,
            "page": page or None,
            "source_type": item["source_type"],
        })
        event, match = _match_tool_call(ctx, item["source_ref"], item["retrieved_by"])
        if event is None:
            missing.append(
                f"no tool_call event references {item['source_ref']!r}"
            )
        else:
            chain.append({
                "hop": "tool_call",
                "event_id": event.event_id,
                "seq": event.seq,
                "agent": event.agent,
                "tool": (event.extra or {}).get("tool") or event.payload_summary,
                "ok": event.ok,
                "duration_ms": event.duration_ms,
                "requested_url": (event.extra or {}).get("requested_url") or "",
                "final_url": (event.extra or {}).get("final_url") or "",
                "match": match,
            })
        chain.append({
            "hop": "agent",
            "agent": item["retrieved_by"],
            "framework": next(
                (event.framework for event in ctx.events if event.agent == item["retrieved_by"]),
                "",
            ),
        })
    chain.append({"hop": "run", "run_id": ctx.run_id})
    return chain, missing


@router.get("/{run_id}/provenance")
def provenance(
    run_id: str,
    request: Request,
    claim: str = Query(default="", description="a claim from the answer, key point or evidence"),
    citation: str = Query(default="", description="1-based citation index or a source_ref"),
) -> dict[str, Any]:
    """Trace a report citation or claim back to a recorded tool call."""
    guard(request)
    if not claim and not citation:
        raise HTTPException(status_code=422, detail="pass claim= or citation=")
    events = _events(run_id)
    ctx = Context(run_id, events, _evidence(run_id))
    report = _section(run_id, "final_report")
    draft = _section(run_id, "draft")
    result: dict[str, Any] = {
        "run_id": run_id,
        "exists": bool(events) or bool(report) or bool(ctx.evidence),
        "query": {"citation": citation or None, "claim": claim or None},
        "citation": None,
        "claim": None,
        "evidence": [],
        "chain": [],
        "missing": [],
        "reason": "",
        "notes": [
            "hops are citation -> evidence item -> source -> tool invocation -> agent -> run",
            "match says how a source reference was matched: exact, normalised, document, "
            "or a clearly labelled fallback",
        ],
    }
    if not events and not report and not ctx.evidence:
        result["reason"] = f"nothing is stored for run {run_id}"
        result["missing"] = ["no events, no final report and no evidence for this run"]
        return result

    if citation:
        index = _citation_index(report, citation.strip())
        if index is None:
            result["reason"] = (
                f"citation {citation!r} does not match any citation of the stored report"
                if report and report.get("citations")
                else "the run has no stored final report with citations"
            )
            result["missing"] = [result["reason"]]
            return result
        chosen = report["citations"][index]
        result["citation"] = {"index": index + 1, **chosen}
        source_ref = str(chosen.get("source_ref") or "")
        matched = [
            {**item, "match": _ref_match(item["source_ref"], source_ref) or ""}
            for item in ctx.evidence
            if _ref_match(item["source_ref"], source_ref)
        ]
    else:
        needle = " ".join(claim.split()).lower()
        result["claim"] = claim
        matched = []
        for item in ctx.evidence:
            haystack = " ".join((item.get("content") or "").split()).lower()
            if needle and needle in haystack:
                matched.append({**item, "match": "content"})
        key_points = [str(point) for point in ((report or {}).get("key_points") or [])]
        claimed_points = [point for point in key_points if needle and needle in point.lower()]
        if claimed_points:
            result["claim_points"] = claimed_points
        based_on = list((draft or {}).get("based_on_evidence_ids") or [])
        if based_on:
            result["draft_evidence_ids"] = based_on
            for item in ctx.evidence:
                if item["id"] in based_on and not any(
                    found["id"] == item["id"] for found in matched
                ):
                    matched.append({**item, "match": "draft_dependency"})
        if not matched:
            result["reason"] = (
                "no stored evidence item contains this claim; it cannot be traced "
                "to a source without inventing a link"
            )
            result["missing"] = [result["reason"]]
            if not claimed_points:
                result["missing"].append(
                    "the claim is not in the stored key points either"
                )
            return result

    result["evidence"] = [
        {
            "id": item["id"],
            "source_type": item["source_type"],
            "source_ref": item["source_ref"],
            "retrieved_by": item["retrieved_by"],
            "provider": item["provider"],
            "score": item["score"],
            "match": item.get("match", ""),
            "snippet": (item.get("content") or "")[:SNIPPET_CHARS],
        }
        for item in matched
    ]
    chain, missing = _provenance_chain(
        ctx, result["citation"], matched
    )
    result["chain"] = chain
    result["missing"] = missing
    return result
