"""Regression tests for the silent-degradation defects in the research pipeline.

Three defects are covered, and each test is offline and deterministic:

1. **Stream redirection killed every MCP tool server.** ADK's ``McpToolset``
   declares ``errlog: TextIO = sys.stderr``, so the value is captured at import
   time. Under ``contextlib.redirect_stderr(<log file>)`` — what the in-process
   evaluation backend did — ``subprocess`` resolved that handle with
   ``msvcrt.get_osfhandle``, the file was closed, and child creation raised
   ``ValueError: I/O operation on closed file``. Every toolset failed to load.
2. **A failed toolset load was silent.** ADK logs a warning and returns an empty
   tool list, so the run gathered no evidence and still reported "completed".
3. **A malformed ``fetch_page`` call with no ``url``** was rejected by the
   provider as a schema violation, with no bounded correction and no record.

The tests never touch the network: the tool servers are replaced by stubs that
fail on demand, and every ``subprocess`` assertion inspects the real handle
objects that MCP would use.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from mcp import StdioServerParameters

from athena.agents import researcher as researcher_module
from athena.agents import toolset_health
from athena.agents.tool_args import (
    FALLBACK_REQUIRED,
    MAX_CORRECTIONS,
    MalformedCallGuard,
    missing_args,
    required_args,
)
from athena.observability.recorder import Recorder, set_recorder
from athena.observability.store import SQLiteEventStore
from athena.summary import RunSummary, finish, summary_line
from athena.tools import mcp_stdio


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _stdio_params(module: str = "athena.tools.web_server") -> StdioConnectionParams:
    return StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=["-m", module],
            env={},
        ),
        timeout=1.0,
    )


def _store(tmp_path: Path) -> SQLiteEventStore:
    return SQLiteEventStore(tmp_path / "events.db")


@pytest.fixture()
def recorder(tmp_path):
    """An installed recorder whose events can be read back."""
    set_recorder(None)
    store = _store(tmp_path)
    rec = Recorder("run-health", store, enabled=True)
    set_recorder(rec)
    try:
        yield rec
    finally:
        set_recorder(None)


def _events(store: SQLiteEventStore, run_id: str = "run-health"):
    return store.events(run_id)


def _health_errors(store: SQLiteEventStore, run_id: str = "run-health"):
    return [
        event
        for event in _events(store, run_id)
        if event.kind == "error" and (event.extra or {}).get("event") == "toolset"
    ]


class _StubTool:
    """A minimal tool object exposing what validation reads."""

    def __init__(self, name: str, required: list[str] | None = None) -> None:
        self.name = name
        schema = {"type": "object", "properties": {}}
        if required is not None:
            schema["required"] = required
        self._mcp_tool = SimpleNamespace(inputSchema=schema)


# --------------------------------------------------------------------------- #
# 1. toolset construction survives process-wide stream redirection
# --------------------------------------------------------------------------- #


def test_errlog_is_a_real_handle_not_the_redirected_stream() -> None:
    """The sink must stay open and usable while ``sys.stderr`` is redirected."""
    mcp_stdio.reset_stderr_sink()
    kwargs = mcp_stdio.toolset_kwargs()
    sink = kwargs["errlog"]

    assert mcp_stdio.usable(sink)
    assert sink is not sys.stderr
    assert sink.fileno() >= 0
    assert os.fstat(sink.fileno())  # a handle the OS still knows

    # A closed log file is the exact object ``redirect_stderr`` handed MCP
    # before this fix: it has a ``closed`` flag and a ``fileno`` that raises.
    log = io.StringIO()
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        assert not mcp_stdio.usable(sys.stderr), "redirected stream has no fileno"
        assert not mcp_stdio.usable(sys.stderr)
    log.close()

    # The sink survived both the redirection and the closing of the log file.
    assert mcp_stdio.usable(sink)
    assert os.fstat(sink.fileno())

    closed = {"closed": True}

    class ClosedLike:
        closed = True

        def fileno(self):
            raise ValueError("I/O operation on closed file")

    assert not mcp_stdio.usable(ClosedLike())
    assert closed["closed"]


def test_building_a_toolset_inside_a_redirect_to_a_closed_file() -> None:
    """The pre-fix failure reproduced: stderr redirected to a file, then closed."""
    mcp_stdio.reset_stderr_sink()
    log_path = Path(mcp_stdio.log_path()).parent / "pipeline-redirect.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")

    with contextlib.redirect_stderr(handle):
        toolset = researcher_module.build_toolset(
            "athena.tools.web_server", tools=["fetch_page"], server="web"
        )
    handle.close()  # the backend closed its per-run log at the end of the run

    assert mcp_stdio.usable(toolset.errlog)
    assert os.fstat(toolset.errlog.fileno())


def test_toolset_kwargs_survive_closed_stderr_under_redirect() -> None:
    """Building a toolset inside a redirect block yields the stable sink."""
    mcp_stdio.reset_stderr_sink()
    log = io.StringIO()
    with contextlib.redirect_stderr(log):
        toolset = researcher_module.build_toolset(
            "athena.tools.web_server",
            tools=["fetch_page"],
            server="web",
            agent="web_researcher",
        )
    log.close()

    errlog = toolset.errlog
    assert mcp_stdio.usable(errlog), "toolset inherited a redirected/closed stream"
    # The same object reached the session manager, which hands it to
    # ``stdio_client(errlog=...)`` and therefore to ``subprocess``.
    assert toolset._mcp_session_manager._errlog is errlog
    assert os.fstat(errlog.fileno())


def test_closed_sys_stderr_does_not_break_the_sink(monkeypatch) -> None:
    """A closed ``sys.stderr.fileno()`` must not poison the sink."""
    mcp_stdio.reset_stderr_sink()

    class ClosedStream:
        closed = True

        def fileno(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stderr", ClosedStream())
    kwargs = mcp_stdio.toolset_kwargs()

    assert "errlog" in kwargs
    assert mcp_stdio.usable(kwargs["errlog"])
    assert isinstance(kwargs["errlog"], io.TextIOBase)


def test_mcp_stdio_usable_rejects_stringio_and_closed_files(tmp_path) -> None:
    assert not mcp_stdio.usable(io.StringIO())
    assert not mcp_stdio.usable(None)
    handle = (tmp_path / "log.txt").open("a", encoding="utf-8")
    assert mcp_stdio.usable(handle)
    handle.close()
    assert not mcp_stdio.usable(handle)


def test_real_subprocess_works_with_the_sink_under_redirect() -> None:
    """End-to-end: the object MCP passes as stderr spawns a real child.

    ``subprocess`` resolves a file-like ``stderr`` with its OS handle, so this
    goes through the same ``get_osfhandle`` path that raised ``ValueError`` for
    the old in-process backend.
    """
    import subprocess

    mcp_stdio.reset_stderr_sink()
    sink = mcp_stdio.toolset_kwargs()["errlog"]
    log = io.StringIO()
    with contextlib.redirect_stderr(log):
        # ``stderr=sink`` is resolved by ``subprocess`` through the same
        # ``get_osfhandle`` path that raised ``ValueError`` when the object was
        # a closed redirect target.
        result = subprocess.run(
            [sys.executable, "-c", "print(42)"],
            stdout=subprocess.PIPE,
            stderr=sink,
            text=True,
            timeout=60,
        )
    log.close()

    assert result.returncode == 0
    assert result.stdout.strip() == "42"


# --------------------------------------------------------------------------- #
# 2. a failed toolset load is loud, never a silent zero-tools run
# --------------------------------------------------------------------------- #


def test_toolset_load_failure_emits_an_error_event(recorder, tmp_path) -> None:
    """A failing load is recorded; it is not swallowed into an empty tool list."""
    toolset = HealthCheckOnly(
        server="web",
        agent="web_researcher",
        required=["fetch_page"],
        # The exact exception the closed-stream defect produced.
        error=ValueError("I/O operation on closed file"),
    )

    with pytest.raises(ValueError, match="closed file"):
        _get_tools(toolset)

    events = _health_errors(recorder.store)
    assert len(events) == 1
    event = events[0]
    assert event.kind == "error"
    assert event.ok is False
    assert event.error_type == "ToolsetLoadFailed"
    assert event.agent == "web_researcher"
    assert event.extra["server"] == "web"
    assert event.extra["degraded"] is True
    assert event.extra["required_tools"] == ["fetch_page"]
    assert event.extra["cause"] == "ValueError"
    assert "closed file" in (event.error_message or "")


def test_toolset_failure_appears_in_failures_and_description(recorder) -> None:
    toolset = HealthCheckOnly(server="web", required=["fetch_page"], error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        _get_tools(toolset)

    rows = toolset_health.failures("run-health")
    assert [row["server"] for row in rows] == ["web"]
    assert rows[0]["cause"] == "RuntimeError"
    lines = toolset_health.describe(rows)
    assert lines == ["toolset web failed to load: boom"]


def test_empty_tool_list_is_a_failure_not_a_clean_load(recorder) -> None:
    """A server that answers with no tools is as unusable as one that died."""
    toolset = HealthCheckOnly(server="rag", required=["rag_search"], tools=[])

    with pytest.raises(RuntimeError, match="exposed no tools"):
        _get_tools(toolset)

    events = _health_errors(recorder.store)
    assert len(events) == 1
    assert events[0].extra["cause"] == "RuntimeError"
    assert "exposed no tools" in (events[0].error_message or "")


def test_healthy_load_records_success_and_no_failure(recorder) -> None:
    """The happy path stays quiet: no error event, one healthy record."""
    toolset = HealthCheckOnly(server="web", required=["fetch_page"], tools=[_StubTool("fetch_page")])

    tools = _get_tools(toolset)

    assert [tool.name for tool in tools] == ["fetch_page"]
    assert _health_errors(recorder.store) == []
    healthy = [
        event
        for event in _events(recorder.store)
        if (event.extra or {}).get("event") == "toolset"
    ]
    assert len(healthy) == 1
    assert healthy[0].ok is True
    assert healthy[0].extra["tools"] == ["fetch_page"]
    assert healthy[0].extra["degraded"] is False


def test_lost_toolset_marks_the_run_summary_degraded(recorder) -> None:
    """A run that lost a toolset can never be reported as a clean success."""
    toolset = HealthCheckOnly(server="web", required=["fetch_page"])
    with pytest.raises(RuntimeError):
        _get_tools(toolset)

    summary = RunSummary(pattern="hierarchical", run_id="run-health")
    summary.outcome = "approved"
    summary.approved = True
    rows = toolset_health.note_on_summary(summary, "run-health")

    assert len(rows) == 1
    assert summary.degraded is True
    assert any("toolset web failed to load" in error for error in summary.stage_errors)

    finish(summary)
    assert summary.degraded is True
    assert "LOST TOOLSETS 1" in summary_line(summary)


def test_toolset_failure_without_recorder_still_prints_to_stderr(capsys) -> None:
    """Outside a run there is no recorder; the loss must still be visible."""
    set_recorder(None)
    toolset_health.record_failure("web", "McpToolset", RuntimeError("nope"), agent="web_researcher")
    captured = capsys.readouterr()
    assert "web failed to load for web_researcher: nope" in captured.err


def test_failures_is_empty_for_a_run_with_no_lost_toolset(recorder) -> None:
    assert toolset_health.failures("run-health") == []


# --------------------------------------------------------------------------- #
# 3. malformed fetch_page call: validation error, one correction, no invention
# --------------------------------------------------------------------------- #


def test_groq_missing_url_is_reported_as_a_validation_error(recorder) -> None:
    """The exact reported failure: ``parameters ... missing properties: 'url'``."""
    tool = _StubTool("fetch_page", required=["url"])
    guard = MalformedCallGuard("web_researcher")

    refusal = guard.check(tool, {})

    assert refusal is not None
    assert refusal["isError"] is True
    assert refusal["error_type"] == "ToolArgsInvalid"
    assert refusal["blocked_by"] == "malformed_args"
    assert "url" in refusal["error"]
    assert refusal["missing"] == ["url"]
    assert refusal["retry_allowed"] is True
    assert refusal["recorded"] == 0, "a malformed call never records evidence"


def test_malformed_call_is_recorded_as_an_error_event(recorder) -> None:
    tool = _StubTool("fetch_page", required=["url"])
    MalformedCallGuard("web_researcher").check(tool, {})

    errors = [
        event
        for event in _events(recorder.store)
        if event.error_type == "MalformedToolCall"
    ]
    assert len(errors) == 1
    assert errors[0].agent == "web_researcher"
    assert errors[0].extra["tool"] == "fetch_page"
    assert errors[0].extra["missing"] == ["url"]
    assert errors[0].extra["attempt"] == 1
    assert errors[0].extra["correction_allowed"] is True


def test_at_most_one_correction_attempt_per_tool(recorder) -> None:
    tool = _StubTool("fetch_page", required=["url"])
    guard = MalformedCallGuard("web_researcher")

    first = guard.check(tool, {})
    second = guard.check(tool, {})

    assert first["retry_allowed"] is True
    assert second["retry_allowed"] is False
    assert "No further `fetch_page` attempts" in second["error"]
    assert guard.attempts["fetch_page"] == 2 == MAX_CORRECTIONS + 1
    assert guard.rejected == 2

    # A third call is still refused, and the allowance never reopens.
    third = guard.check(tool, {})
    assert third["retry_allowed"] is False
    assert guard.attempts["fetch_page"] == 3


def test_a_corrected_call_passes_and_records_nothing(recorder) -> None:
    tool = _StubTool("fetch_page", required=["url"])
    guard = MalformedCallGuard("web_researcher")

    assert guard.check(tool, {}) is not None
    assert guard.check(tool, {"url": "https://nasa.gov/topic"}) is None
    assert guard.rejected == 1
    errors = [
        event
        for event in _events(recorder.store)
        if event.error_type == "MalformedToolCall"
    ]
    assert len(errors) == 1, "only the malformed attempt is recorded"


def test_guard_never_invents_a_missing_url(recorder) -> None:
    """Guidance must refuse to supply a value, and must carry no URL of its own."""
    tool = _StubTool("fetch_page", required=["url"])
    guard = MalformedCallGuard("web_researcher")

    refusal = guard.check(tool, {"max_chars": 4000})
    message = refusal["error"]

    assert "http" not in message.lower(), "the guard must not fabricate a URL"
    assert "Do not invent a value" in message
    assert "if you cannot supply url" in message


def test_empty_and_whitespace_urls_are_malformed() -> None:
    tool = _StubTool("fetch_page", required=["url"])
    guard = MalformedCallGuard("web_researcher")

    for args in ({"url": ""}, {"url": "   "}, {"url": None}, {"max_chars": 100}):
        refusal = guard.check(tool, args)
        assert refusal is not None, f"{args!r} should have been rejected"
        assert refusal["missing"] == ["url"]


def test_non_dict_arguments_are_rejected_as_malformed(recorder) -> None:
    tool = _StubTool("fetch_page", required=["url"])
    guard = MalformedCallGuard("web_researcher")

    refusal = guard.check(tool, "https://nasa.gov")
    assert refusal is not None
    assert refusal["missing"] == ["url"]


def test_required_args_prefers_the_tool_schema_and_falls_back_honestly() -> None:
    declared = _StubTool("fetch_page", required=["url", "max_chars"])
    assert required_args(declared) == ["url", "max_chars"]

    # No usable schema: the published required list for that tool is used.
    plain = SimpleNamespace(name="fetch_page")
    assert required_args(plain) == ["url"]
    assert FALLBACK_REQUIRED["fetch_page"] == ("url",)
    assert missing_args(plain, {}) == ["url"]
    assert missing_args(plain, {"url": "https://x.gov"}) == []


def test_fallback_required_matches_the_real_mcp_schemas() -> None:
    """The fallback map must mirror what the servers actually declare."""
    tools = {
        "web_search": ["query"],
        "fetch_page": ["url"],
        "calculate": ["expression"],
        "forecast": ["values", "horizon"],
        "rag_search": ["query"],
    }
    for name, expected in tools.items():
        assert list(FALLBACK_REQUIRED[name]) == expected


def test_malformed_guard_is_wired_into_the_researcher_callback(recorder) -> None:
    """The real composed callback refuses a URL-less fetch_page call."""
    guard = researcher_module._compose_guard(
        {"fetch_page"}, "web_researcher", lambda tool, args, ctx: None
    )

    refusal = guard(_StubTool("fetch_page", required=["url"]), {}, None)

    assert refusal is not None
    assert refusal["error_type"] == "ToolArgsInvalid"
    assert refusal["missing"] == ["url"]
    # The malformed call short-circuits before the budget/URL-normalising
    # callback, so nothing downstream can invent a URL.
    assert refusal["recorded"] == 0


def test_composed_guard_still_allows_a_valid_fetch(recorder) -> None:
    seen: list[dict] = []

    def budget(tool, args, ctx):
        seen.append(dict(args))
        return None

    guard = researcher_module._compose_guard({"fetch_page"}, "web_researcher", budget)
    tool = _StubTool("fetch_page", required=["url"])

    assert guard(tool, {"url": "https://www.nasa.gov/x"}, None) is None
    assert seen == [{"url": "https://www.nasa.gov/x"}]


def test_composed_guard_still_refuses_an_unassigned_tool(recorder) -> None:
    """Defence in depth survives: an out-of-role tool is still refused.

    ``run_enabled`` mirrors the run's enabled set. With ``web_search`` enabled
    for the run but not assigned to this worker, the refusal names the worker
    assignment rather than blaming the user's selection.
    """
    guard = researcher_module._compose_guard(
        {"fetch_page"},
        "web_researcher",
        lambda tool, args, ctx: None,
        {"fetch_page", "web_search"},
    )
    refusal = guard(_StubTool("web_search", required=["query"]), {"query": "nasa"}, None)
    assert refusal["error_type"] == "ToolNotAssigned"
    assert refusal["blocked_by"] == "worker_assignment"


def test_composed_guard_reports_a_user_disabled_tool(recorder) -> None:
    guard = researcher_module._compose_guard(
        {"fetch_page"},
        "web_researcher",
        lambda tool, args, ctx: None,
        {"fetch_page"},
    )
    refusal = guard(_StubTool("web_search", required=["query"]), {"query": "nasa"}, None)
    assert refusal["error_type"] == "ToolDisabled"
    assert refusal["blocked_by"] == "user_selection"


def test_malformed_fetch_yields_no_evidence_anywhere(recorder, tmp_path) -> None:
    """The whole point: a refused call records no evidence, and none is invented."""
    from athena.agents.tool_report import build_tool_report
    from athena.blackboard import Blackboard, SQLiteStore, Task

    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="Which element has symbol W?"), store=store)
    set_recorder(Recorder(board.run_id, recorder.store, enabled=True))

    guard = researcher_module._compose_guard(
        {"fetch_page"}, "web_researcher", lambda tool, args, ctx: None
    )
    guard(_StubTool("fetch_page", required=["url"]), {}, None)
    guard(_StubTool("fetch_page", required=["url"]), {}, None)

    assert board.read("evidence") is None
    report = build_tool_report(board.run_id, store)
    assert report["calls"] == []
    assert report["invoked"] == []
    assert report["usable_evidence"] == []


# --------------------------------------------------------------------------- #
# harness: a toolset whose load can be made to fail
# --------------------------------------------------------------------------- #


class HealthCheckOnly(toolset_health.HealthCheckedToolset):
    """A real ``HealthCheckedToolset`` whose transport is replaced by a stub.

    ``_load`` is what the patched ``McpToolset.get_tools`` calls, so a test can
    make the underlying load fail or return an arbitrary tool list without a
    subprocess or a network call.
    """

    def __init__(self, *, server="", agent="researcher", required=(), tools=None, error=None):
        super().__init__(connection_params=_stdio_params())
        self.use_health(server=server, agent=agent, required=list(required))
        self._stub_tools = list(tools or [])
        self._stub_error = error
        self._mcp_session_manager = None

    def _load(self, readonly_context=None):
        if self._stub_error is not None:
            raise self._stub_error
        return list(self._stub_tools)


def _get_tools(toolset) -> list:
    """Run the real ``HealthCheckedToolset.get_tools`` against a stubbed load.

    ``McpToolset.get_tools`` needs a live transport, so the parent
    implementation is patched to call the stub's ``_load``. That leaves the
    real recording logic under test rather than a reimplementation of it.
    """
    import asyncio

    original = McpToolset.get_tools

    async def patched(self, readonly_context=None):
        return self._load(readonly_context)

    McpToolset.get_tools = patched
    try:
        return asyncio.run(toolset.get_tools(None))
    finally:
        McpToolset.get_tools = original


def test_health_checked_toolset_is_a_real_mcp_toolset() -> None:
    """ADK only treats a toolset as a toolset if it is one."""
    from google.adk.tools.base_toolset import BaseToolset

    toolset = researcher_module.build_toolset("athena.tools.web_server", tools=["fetch_page"])
    assert isinstance(toolset, BaseToolset)
    assert isinstance(toolset, McpToolset)
    assert is_toolset_like(toolset)


def is_toolset_like(toolset) -> bool:
    """The attribute the lifecycle close path relies on."""
    return hasattr(toolset, "get_tools") and hasattr(toolset, "close")
