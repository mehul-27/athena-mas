"""The demo API: one FastAPI app the showcase page talks to.

Read-only apart from ``POST /api/runs``. Everything it returns comes from the
event store or the blackboard; nothing is recomputed from provider data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from athena.agents.a2a_stage import DEFAULT_CARD_URL
from athena.agents.tool_report import build_tool_report
from athena.api import replay
from athena.api.auth import AccessTokenMiddleware
from athena.api.auth import router as auth_router
from athena.api.guards import guard
from athena.api.health_routes import build_lifespan, configure_logging
from athena.api.health_routes import router as health_router
from athena.api.runs import BusyError, RunRegistry
from athena.blackboard import Blackboard
from athena.guardrails.input import refusal_message, screen_input
from athena.guardrails.secrets import redact_tree
from athena.observability.ambient import (
    RunIdScopeMiddleware,
    install_process_recorder,
)
from athena.observability.guardrail_events import record_flags
from athena.observability.heatmap import render_heatmap
from athena.observability.metrics import agent_metrics
from athena.observability.redact import redact
from athena.observability.store import SQLiteEventStore
from athena.patterns import VALID_PATTERNS
from athena.tools import registry as tool_registry

STATIC = Path(__file__).parent / "static"
QUESTIONS_CSV = Path("data/eval/nasa_rag_40_questions.csv")
DEMO_QUESTIONS = Path("data/demo/questions.json")
STREAM_POLL_S = 0.3
STREAM_MAX_S = 1800
STREAM_EXIT_WAIT_S = 5.0

# Named with a leading underscore: ``create_app`` has a route function called
# ``log``, which would shadow a module-level ``log`` inside that scope.
_LOG = logging.getLogger("athena.api")

REGISTRY = RunRegistry()


class RunRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    pattern: str = Field(default="sequential")
    tools: list[str] | None = Field(
        default=None,
        max_length=16,
        description=(
            "Research tool ids enabled for this run. Omit or send null for All "
            "tools; send [] for no tools."
        ),
    )


def _store() -> SQLiteEventStore:
    return SQLiteEventStore()


def _record_guardrails(run_id: str, verdict) -> None:
    """Write one ``guardrail`` event per flag, so the run's trace shows them.

    This goes through :func:`athena.observability.guardrail_events.record_flags`
    — the same helper every guardrail module uses — so the row shape and the
    redaction of ``detail`` are guaranteed by one implementation instead of a
    second copy here. ``screen`` names this endpoint because screening happens
    before the run exists, so the endpoint is what attributes the flags.
    """
    record_flags(
        getattr(verdict, "flags", ()) or (),
        screen="POST /api/runs",
        pipeline_stage="input",
        run_id=run_id,
    )


def _evidence_attribution(run_id: str) -> dict[str, list[str]]:
    """Map a run to the roles that recorded its evidence.

    The metrics table attributes evidence through these entries. Passing an
    empty map made every row show ``evidence_items=0`` even though the
    Blackboard contained evidence writes.
    """
    found = Blackboard.open(run_id).read("evidence")
    return {
        run_id: [
            item.retrieved_by
            for item in (found[0] if found else [])
        ]
    }


def _crew_server_up(timeout: float = 1.0) -> bool:
    import httpx

    try:
        return httpx.get(DEFAULT_CARD_URL, timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001 - unreachable means "down"
        return False


def _event_payload(event) -> dict:
    data = json.loads(event.to_json())
    data["summary"] = redact(event.payload_summary or "")[:300]
    return data


def _snapshot(run_id: str, runs: RunRegistry | None = None) -> dict:
    blackboard = Blackboard.open(run_id)
    if blackboard.read("task") is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id}")

    def section(name: str):
        found = blackboard.read(name)
        if not found:
            return None
        # Snapshot sections carry generated prose (``final_report``, ``draft``…)
        # and can quote a tool payload. Redact field by field rather than trusting
        # every section to have been clean on the way in.
        return {"value": redact_tree(found[0].model_dump(mode="json")), "version": found[1]}

    evidence = blackboard.read("evidence")
    return {
        "run_id": run_id,
        "summary": _summary(run_id, runs),
        "task": section("task"),
        "plan": section("plan"),
        "analysis": section("analysis"),
        "draft": section("draft"),
        "critique": section("critique"),
        "final_report": section("final_report"),
        "evidence": [
            {
                "id": item.id,
                "source_type": item.source_type,
                "source_ref": item.source_ref,
                "score": item.score,
                "retrieved_by": item.retrieved_by,
                "text": redact(item.content),
                "content": redact(item.content),
            }
            for item in (evidence[0] if evidence else [])
        ],
        "tool_report": build_tool_report(run_id, blackboard.store),
        "history": [
            {"seq": entry["seq"], "author": entry["author"],
             "section": entry["section"], "version": entry["version"]}
            for entry in blackboard.history()
        ],
    }


def _summary(run_id: str, runs: RunRegistry | None = None) -> dict:
    for event in reversed(_store().events(run_id)):
        if event.payload_summary and event.payload_summary.startswith("run_summary"):
            summary = dict(event.extra)
            if not summary.get("critic_status"):
                from athena.summary import critique_details

                critique = Blackboard.open(run_id).read("critique")
                status, reason = critique_details(
                    critique[0] if critique else None,
                    summary.get("stage_errors") or [],
                )
                summary["critic_status"] = status
                summary["critique_reason"] = reason
            if "evidence_sufficient" not in summary or "outcome" not in summary:
                from athena.agents.sufficiency import assess

                blackboard = Blackboard.open(run_id)
                task = blackboard.read("task")
                evidence = blackboard.read("evidence")
                assessment = assess(
                    task[0].question if task else "",
                    evidence[0] if evidence else [],
                    enabled_tools=task[0].tools if task else None,
                )
                summary.setdefault("evidence_sufficient", assessment.ok)
                summary.setdefault("unmet_requirements", list(assessment.issues))
                summary.setdefault("evidence_limitations", list(assessment.limitations))
                if "outcome" not in summary:
                    approved = summary.get("approved")
                    if summary.get("failed"):
                        summary["outcome"] = "failed"
                    elif summary.get("critic_status") in {"failed", "missing"}:
                        summary["outcome"] = "incomplete"
                    elif approved is False:
                        summary["outcome"] = "rejected"
                    elif approved is True and assessment.ok:
                        summary["outcome"] = "approved"
                    else:
                        summary["outcome"] = "incomplete"
                    if summary["outcome"] != "approved":
                        summary["degraded"] = True
            return _with_process_status(summary, run_id, runs)
    return {}


def _safe_refresh(runs: RunRegistry | None) -> object | None:
    try:
        return (runs or REGISTRY).refresh()
    except Exception:  # noqa: BLE001 - status lookup must not break a response
        return None


def _with_process_status(
    summary: dict, run_id: str, runs: RunRegistry | None
) -> dict:
    """Make a run_summary agree with the subprocess's actual terminal state."""
    active = _safe_refresh(runs)
    if active is None or getattr(active, "run_id", None) != run_id:
        return summary

    result = dict(summary)
    if not getattr(active, "finished", False):
        # A summary event was written, but the process has not exited yet. Do
        # not call that a confirmed success; the SSE path waits briefly and
        # then renders this as an incomplete completion.
        if not result.get("failed"):
            result["degraded"] = True
            result["outcome"] = "incomplete"
            result["stage_errors"] = list(result.get("stage_errors") or []) + [
                "run summary is present but the process has not exited"
            ]
        return result

    exit_code = getattr(active, "exit_code", None)
    if exit_code not in (None, 0) and not result.get("failed"):
        detail = f"run process exited with code {exit_code} after run summary"
        result["failed"] = True
        result["degraded"] = True
        result["outcome"] = "failed"
        result["stage_errors"] = list(result.get("stage_errors") or []) + [detail]
        # A run summary may contain a critic approval recorded before the
        # process exit. Do not present it as an approved result after failure.
        result["approved"] = None
        result["severity"] = None
        result["critique_reason"] = detail + "; this is not an approval"
    return result


def create_app(registry: RunRegistry | None = None) -> FastAPI:
    configure_logging()
    # The API process is not a research run, so nothing installed a recorder in
    # it and every guardrail decision made here (the chat turn's input, tool
    # output and answer screening) was written to no store at all. This installs
    # one process-level recorder; it never displaces a run's own recorder and it
    # cannot change a decision (see athena/observability/ambient.py).
    install_process_recorder()
    runs = registry or REGISTRY
    # The lifespan creates the writable data directories at startup and stops
    # the active run plus the child crew server on SIGTERM (see health_routes).
    app = FastAPI(title="Athena demo", lifespan=build_lifespan(runs))

    @app.get("/api/health")
    def health() -> dict:
        from athena.api.activation import active_summary

        active = active_summary()
        has_profile_key = bool(active and active.get("has_key"))
        effective_model = (active or {}).get("model") or os.getenv("LLM_MODEL", "")
        effective_host = (active or {}).get("api_host") or _host_only(
            os.getenv("LLM_API_BASE", "")
        )
        return {
            "crew_server": "up" if _crew_server_up() else "down",
            "provider_configured": bool(os.getenv("LLM_API_KEY")) or has_profile_key,
            "active_profile": active,
            "model": effective_model,
            "api_host": effective_host,
            "replay_available": len(replay.names()),
            "patterns": list(VALID_PATTERNS),
        }

    @app.get("/api/questions")
    def questions() -> dict:
        return {"demo": _demo_questions(), "eval": _eval_questions()}

    @app.post("/api/runs")
    def start_run(payload: RunRequest, request: Request) -> dict:
        guard(request)
        if payload.pattern not in VALID_PATTERNS:
            raise HTTPException(status_code=422, detail=f"unknown pattern {payload.pattern!r}")
        # ``emit=False``: this endpoint creates the run id *after* screening, so
        # it cannot attribute the rows yet. ``_record_guardrails`` below writes
        # them once the run exists — exactly once, never twice.
        verdict = screen_input(payload.question, emit=False)
        if verdict.blocking:
            _LOG.warning(
                "run request blocked by guardrails: %s",
                ", ".join(verdict.categories) or "blocked",
            )
            raise HTTPException(status_code=422, detail=refusal_message(verdict))
        try:
            tools = tool_registry.normalise_selection(payload.tools)
        except tool_registry.UnknownToolError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            run_id = runs.start(
                payload.question,
                payload.pattern,
                list(tools) if tools is not None else None,
            )
        except BusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _record_guardrails(run_id, verdict)
        return {
            "run_id": run_id,
            "guardrail_flags": [flag.model_dump() for flag in verdict.flags],
        }

    @app.get("/api/runs/{run_id}")
    def snapshot(run_id: str) -> dict:
        return _snapshot(run_id, runs)

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: str) -> dict:
        """Persisted events, for restoring a completed run's trace."""
        return {
            "run_id": run_id,
            "events": [_event_payload(event) for event in _store().events(run_id)],
        }

    @app.get("/api/runs/{run_id}/metrics")
    def metrics(run_id: str) -> dict:
        rows = agent_metrics(
            [run_id], {run_id: _store().events(run_id)}, _evidence_attribution(run_id)
        )
        return {"run_id": run_id, "rows": [row.as_dict() for row in rows]}

    @app.get("/api/runs/{run_id}/heatmap.png")
    def heatmap(run_id: str):
        rows = agent_metrics(
            [run_id], {run_id: _store().events(run_id)}, _evidence_attribution(run_id)
        )
        target = Path(".run/api") / f"{run_id}.png"
        render_heatmap(rows, target, f"Athena {run_id[:8]}")
        return FileResponse(target, media_type="image/png")

    @app.get("/api/runs/{run_id}/stream")
    def stream(run_id: str, request: Request) -> StreamingResponse:
        try:
            last_event_id = int(request.headers.get("last-event-id") or "0")
        except ValueError:
            last_event_id = 0
        return StreamingResponse(
            _events(run_id, runs, last_event_id),
            media_type="text/event-stream",
        )

    @app.get("/api/runs/{run_id}/log")
    def log(run_id: str) -> dict:
        run = runs.refresh()
        if run and run.run_id == run_id:
            return {"run_id": run_id, "finished": run.finished,
                    "exit_code": run.exit_code, "lines": run.lines[-400:]}
        path = Path(".run/runs") / f"{run_id}.log"
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        return {"run_id": run_id, "finished": True, "exit_code": None,
                "lines": [redact(x) for x in text.splitlines()[-400:]]}

    @app.get("/api/compare")
    def compare(run: str = "") -> dict:
        ids = [value for value in run.split(",") if value] if run else []
        return {"rows": [_compare_row(run_id, runs) for run_id in ids]}

    @app.get("/api/runs")
    def history() -> dict:
        store = _store()
        runs = []
        for run_id in store.runs():
            summary = _summary(run_id, runs)
            if not summary:
                continue
            task = Blackboard.open(run_id).read("task")
            question = task[0].question if task else ""
            runs.append({
                "run_id": run_id,
                "question": question,
                "pattern": summary.get("pattern"),
                "started_at": summary.get("started_at") or "",
                "confidence": summary.get("confidence"),
                "approved": summary.get("approved"),
                "severity": summary.get("severity"),
                "critic_status": summary.get("critic_status"),
                "critique_reason": summary.get("critique_reason"),
                "evidence_sufficient": summary.get("evidence_sufficient"),
                "outcome": summary.get("outcome"),
                "degraded": summary.get("degraded"),
            })
        return {"runs": list(reversed(runs))[-50:]}

    @app.get("/api/tools")
    def tools() -> dict:
        return {"servers": _tool_catalog()}

    @app.get("/api/research/tools")
    def research_tools() -> dict:
        """The canonical research selector catalogue."""
        from athena.api.activation import active_env

        provider = active_env().get("SEARCH_PROVIDER") or None
        return {
            "tools": tool_registry.catalog(provider),
            "all_tools": list(tool_registry.ALL_TOOL_IDS),
        }

    @app.get("/api/replay")
    def replay_index() -> dict:
        return {"bundles": replay.index()}

    @app.get("/api/replay/{name}")
    def replay_bundle(name: str) -> dict:
        try:
            return replay.load(name)
        except replay.ReplayError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.exception_handler(Exception)
    async def unhandled(request, exc: Exception):  # noqa: ANN001 - FastAPI hook
        _LOG.exception(
            "unhandled error on %s %s",
            request.method,
            request.url.path,
            extra={"event": "unhandled", "method": request.method, "path": request.url.path},
        )
        detail = redact(f"{type(exc).__name__}: {exc}") or "internal error"
        return JSONResponse(status_code=500, content={"detail": detail[:300]})

    from athena.api.chat_routes import router as chat_router
    from athena.api.eval_routes import router as eval_router
    from athena.api.library_routes import documents_router as documents_router
    from athena.api.library_routes import router as library_router
    from athena.api.settings_routes import router as settings_router
    from athena.api.telemetry_routes import router as telemetry_router

    # Probe/login routes first: /healthz and /readyz must answer before the
    # static mount and outside the optional token gate.
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(settings_router)
    app.include_router(chat_router)
    app.include_router(library_router)
    app.include_router(documents_router)
    app.include_router(telemetry_router)
    app.include_router(eval_router)
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")

    # No-op unless ATHENA_ACCESS_TOKEN is set; exempt paths and static assets
    # are handled inside the middleware.
    app.add_middleware(AccessTokenMiddleware)
    # Outermost of the two: it puts the request's own run id (a chat turn's
    # conversation id) in scope so the guardrail rows written while handling
    # this request are attributed to it, and every other request is untouched.
    app.add_middleware(RunIdScopeMiddleware)
    return app


async def _wait_for_exit(
    runs: RunRegistry, run_id: str, timeout: float
) -> None:
    """Wait briefly for the subprocess after its run_summary event.

    Without this, a process that writes its summary and then crashes could be
    reported as successful before its nonzero exit code is visible.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        active = _safe_refresh(runs)
        if active is None or getattr(active, "run_id", None) != run_id:
            return
        if getattr(active, "finished", False):
            return
        await asyncio.sleep(0.05)


async def _events(run_id: str, runs: RunRegistry | None = None, last_event_id: int = 0):
    """SSE: replay what exists, then follow the event log until done.

    A response may already have been sent before the run's Blackboard task is
    created. Never raise an HTTPException from this generator: wait while the
    accepted run is still starting, and emit a terminal failed ``done`` event if
    the run is no longer recoverable.
    """
    store = _store()
    registry = runs or REGISTRY
    last_seq = max(0, int(last_event_id or 0))
    history_seen = 0
    deadline = time.monotonic() + STREAM_MAX_S

    while time.monotonic() < deadline:
        try:
            events = store.events(run_id)
        except Exception as exc:  # noqa: BLE001 - headers are already sent
            detail = f"stream could not read the event log: {type(exc).__name__}"
            try:
                active = registry.refresh()
            except Exception:  # noqa: BLE001
                active = None
            yield f"event: done\ndata: {json.dumps({'summary': _unavailable_summary(run_id, active, detail)})}\n\n"
            return
        for event in events:
            if event.seq <= last_seq:
                continue
            last_seq = event.seq
            yield f"id: {event.seq}\ndata: {json.dumps(_event_payload(event))}\n\n"

        try:
            snapshot = _snapshot(run_id, registry)
        except HTTPException:
            try:
                active = registry.refresh()
            except Exception:  # noqa: BLE001 - unavailable is still a terminal state
                active = None
            if active and active.run_id == run_id and not active.finished:
                # The subprocess is alive but has not created its task yet.
                await asyncio.sleep(STREAM_POLL_S)
                continue
            detail = (
                "the run is no longer available and no terminal result can be "
                "recovered"
            )
            yield f"event: done\ndata: {json.dumps({'summary': _unavailable_summary(run_id, active, detail)})}\n\n"
            return
        except Exception as exc:  # noqa: BLE001 - streaming has already started
            detail = f"stream could not read the run: {type(exc).__name__}"
            try:
                active = registry.refresh()
            except Exception:  # noqa: BLE001
                active = None
            yield f"event: done\ndata: {json.dumps({'summary': _unavailable_summary(run_id, active, detail)})}\n\n"
            return

        if len(snapshot["history"]) > history_seen:
            history_seen = len(snapshot["history"])
            yield f"event: blackboard\ndata: {json.dumps({'history': snapshot['history'][-1]})}\n\n"

        done = any(
            event.payload_summary and event.payload_summary.startswith("run_summary")
            for event in events
        )
        if done:
            await _wait_for_exit(registry, run_id, STREAM_EXIT_WAIT_S)
            try:
                summary = _summary(run_id, registry)
            except Exception as exc:  # noqa: BLE001 - never raise after headers
                try:
                    active = registry.refresh()
                except Exception:  # noqa: BLE001
                    active = None
                detail = f"terminal summary unavailable: {type(exc).__name__}"
                summary = _unavailable_summary(run_id, active, detail)
            yield f"event: done\ndata: {json.dumps({'summary': summary})}\n\n"
            return
        await asyncio.sleep(STREAM_POLL_S)

    yield "event: error\ndata: " + json.dumps({"detail": "stream timed out"}) + "\n\n"


def _unavailable_summary(run_id: str, active, detail: str) -> dict:
    """A failed terminal summary for a run with no recoverable Blackboard task."""
    return {
        "pattern": getattr(active, "pattern", "") if active else "",
        "run_id": run_id,
        "failed": True,
        "degraded": True,
        "outcome": "failed",
        "stage_errors": [detail],
        "approved": None,
        "severity": None,
        "confidence": None,
        "critique_reason": "run unavailable; this is not an approval",
        "evidence_sufficient": False,
    }


def _host_only(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).hostname or "" if url else ""


def _demo_questions() -> list[dict]:
    if not DEMO_QUESTIONS.exists():
        return []
    return json.loads(DEMO_QUESTIONS.read_text(encoding="utf-8"))


def _eval_questions() -> list[dict]:
    if not QUESTIONS_CSV.exists():
        return []
    import csv

    with QUESTIONS_CSV.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [{"id": row.get("id") or row.get("question_id") or str(index + 1),
             "text": row.get("question") or row.get("text") or ""}
            for index, row in enumerate(rows)]


def _compare_row(run_id: str, runs: RunRegistry | None = None) -> dict:
    summary = _summary(run_id, runs)
    return {
        "run_id": run_id,
        "pattern": summary.get("pattern"),
        "wall_s": summary.get("wall_s"),
        "adk_tokens": summary.get("adk_tokens"),
        "a2a_bytes_out": summary.get("a2a_bytes_out"),
        "a2a_bytes_in": summary.get("a2a_bytes_in"),
        "evidence_count": summary.get("evidence_count"),
        "tool_calls": summary.get("tool_calls"),
        "revision_rounds": summary.get("revision_rounds"),
        "confidence": summary.get("confidence"),
        "approved": summary.get("approved"),
        "severity": summary.get("severity"),
        "critic_status": summary.get("critic_status"),
        "critique_reason": summary.get("critique_reason"),
        "evidence_sufficient": summary.get("evidence_sufficient"),
        "outcome": summary.get("outcome"),
        "degraded": summary.get("degraded"),
    }

_TOOLS_CACHE: list[dict] | None = None


def _tool_catalog() -> list[dict]:
    """Ask the MCP servers what they expose, once per process."""
    global _TOOLS_CACHE
    import subprocess
    import sys

    if _TOOLS_CACHE is not None:
        return _TOOLS_CACHE
    result = subprocess.run(
        [sys.executable, "scripts/list_tools.py"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        _TOOLS_CACHE = []
        return _TOOLS_CACHE
    try:
        _TOOLS_CACHE = json.loads(result.stdout).get("servers", [])
    except ValueError:
        _TOOLS_CACHE = []
    return _TOOLS_CACHE