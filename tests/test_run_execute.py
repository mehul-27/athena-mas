"""End-to-end drive of ``athena.run._execute`` with a stub runner.

No ADK, no model, no server: a stub runner yields a small, realistic event
sequence, which is enough to exercise the lines that crashed in Step 6c
(``parts_of`` / tool counting) and the failure-warning path from Step 6d.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import athena.run as runner_mod


def _part(text=None, function_call=None, function_response=None):
    return SimpleNamespace(
        text=text, thought=False,
        function_call=function_call, function_response=function_response,
    )


def _event(author, *, parts=(), metadata=None, usage=None):
    return SimpleNamespace(
        author=author,
        content=SimpleNamespace(parts=list(parts)),
        custom_metadata=metadata or {},
        usage_metadata=usage,
    )


class _Sessions:
    async def create_session(self, **_kwargs):
        return SimpleNamespace(id="session-1")


class StubRunner:
    """Stands in for InMemoryRunner: same attributes _execute touches."""

    def __init__(self, events):
        self.session_service = _Sessions()
        self._events = events

    async def run_async(self, **_kwargs):
        for event in self._events:
            yield event


def _events() -> list:
    failing_ack = json.dumps(
        {"ok": False, "role": "analyst", "error_type": "TypeError",
         "error": "Completions.parse() got an unexpected keyword argument"}
    )
    return [
        # The line that crashed: a tool call part.
        _event("researcher", parts=[_part(function_call=SimpleNamespace(
            name="rag_search", args={"query": "nasa", "top_k": 3}))]),
        _event("researcher", parts=[_part(function_response=SimpleNamespace(
            name="rag_search", response={"recorded": 3}))]),
        # Token accounting.
        _event("researcher", usage=SimpleNamespace(
            prompt_token_count=1200, candidates_token_count=40)),
        # Stage timing event from the supervisor.
        _event("supervisor", metadata={"stage": "planner", "stage_ms": 1500}),
        # A CrewAI role that failed, as the ADK side sees it.
        _event("analyst", parts=[_part(text=failing_ack)]),
        _event("supervisor", parts=[_part(text="# Athena Research Report")],
               metadata={"final": True}),
    ]


def test_execute_counts_tools_and_tokens(tmp_path, monkeypatch, capsys) -> None:
    stub = StubRunner(_events())
    summary = asyncio.run(
        runner_mod._execute(
            "What is systems engineering?", "sequential", verbose=True,
            runner_factory=lambda run_id, pattern, recorder: stub,
        )
    )

    assert summary.tool_calls == 1
    assert summary.adk_tokens == 1200
    assert summary.stage_ms == {"planner": 1500.0}
    assert summary.wall_s >= 0


def test_execute_marks_a_failed_role_and_warns(tmp_path, monkeypatch, capsys) -> None:
    stub = StubRunner(_events())
    summary = asyncio.run(
        runner_mod._execute(
            "What is systems engineering?", "sequential", verbose=True,
            runner_factory=lambda run_id, pattern, recorder: stub,
        )
    )

    printed = capsys.readouterr().out
    assert "WARNING analyst failed: TypeError" in printed
    assert "unexpected keyword" in printed
    assert summary.stage_errors == ["analyst: TypeError"]
    assert summary.degraded is True


def test_execute_is_quiet_when_verbose_is_off(capsys) -> None:
    stub = StubRunner(_events())
    asyncio.run(
        runner_mod._execute(
            "q", "sequential", verbose=False,
            runner_factory=lambda run_id, pattern, recorder: stub,
        )
    )
    printed = capsys.readouterr().out
    assert "WARNING analyst failed" not in printed
    assert "stages:" not in printed


def test_default_runner_is_used_when_no_factory_is_given() -> None:
    """The seam must be optional: production still builds the real runner."""
    assert callable(runner_mod.default_runner)
    assert runner_mod.default_runner is not None
