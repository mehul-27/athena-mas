"""Deterministic regression tests for run lifecycle (item F).

Two promises are pinned here:

* every run ends in exactly one terminal state (success / failure / cancelled /
  timeout) and that state reaches the summary;
* no child work survives the stage that owned it - worker coroutines are
  cancelled, tool-server toolsets are closed, and cleanup runs in ``finally`` on
  success, failure, cancellation and timeout alike.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import athena.run as runner_mod
from athena.agents.lifecycle import (
    StageTimeout,
    cancel_tasks,
    close_toolsets,
    normalise_terminal,
    run_bounded,
)
from athena.agents.supervisor import SupervisorAgent
from athena.blackboard import (
    Blackboard,
    Citation,
    Critique,
    EvidenceItem,
    FinalReport,
    SQLiteStore,
    Task,
)
from athena.patterns.parallel import ConcurrentWorkers
from athena.summary import (
    TERMINAL_STATES,
    RunSummary,
    finish,
    set_outcome,
    set_terminal,
)

QUESTION = "What changed in Python 3.13?"
SOURCE = "https://docs.python.org/3.13/whatsnew/3.13.html"
EVIDENCE = (
    "The Python 3.13 release notes describe the changed behavior and the new "
    "features in that release. "
)


# --- the terminal state is a single, well-defined value ---------------------


def test_normalise_terminal_maps_every_outcome_onto_four_states() -> None:
    assert normalise_terminal("approved") == "success"
    assert normalise_terminal("cancelled") == "cancelled"
    assert normalise_terminal("timed_out") == "timeout"
    assert normalise_terminal("") == "failure"
    for value in TERMINAL_STATES:
        assert normalise_terminal(value) == value


def test_the_first_terminal_state_wins_and_a_late_one_is_recorded() -> None:
    summary = RunSummary(pattern="sequential")

    assert set_terminal(summary, "cancelled", "the user cancelled") == "cancelled"
    assert set_terminal(summary, "success", "the stage finished anyway") == "cancelled"

    assert summary.terminal_state == "cancelled"
    assert "ignored success" in summary.terminal_reason


def test_a_run_with_failed_stages_is_degraded_and_still_terminal() -> None:
    summary = RunSummary(pattern="sequential")
    summary.stage_errors.append("critic: RuntimeError")

    finish(summary)

    assert summary.terminal_state in TERMINAL_STATES
    assert summary.degraded is True


def test_set_outcome_never_reports_success_without_approval_and_evidence() -> None:
    approved = RunSummary(pattern="sequential")
    set_outcome(approved, report_present=True, evidence_sufficient=True)
    # approved is still None: no critique is not an approval.
    assert approved.outcome == "incomplete"
    assert approved.terminal_state == "failure"

    good = RunSummary(pattern="sequential")
    good.approved = True
    set_outcome(good, report_present=True, evidence_sufficient=True)
    assert good.outcome == "approved"
    assert good.terminal_state == "success"


# --- driving a whole run through _execute ----------------------------------


class _Sessions:
    async def create_session(self, **_kwargs):
        return SimpleNamespace(id="session-1")


def _final_event() -> SimpleNamespace:
    return SimpleNamespace(
        author="supervisor", content=None, custom_metadata={"final": True},
        usage_metadata=None,
    )


class _Runner:
    """Stands in for InMemoryRunner, writing blackboard state then returning."""

    def __init__(self, run_id: str, *, evidence: str | None, critique: bool) -> None:
        self.session_service = _Sessions()
        self.run_id = run_id
        self.evidence = evidence
        self.critique = critique

    async def run_async(self, **_kwargs):
        board = Blackboard.open(self.run_id)
        if self.evidence:
            board.append(
                "evidence",
                EvidenceItem(
                    id="fetch-1",
                    source_type="web",
                    source_ref=SOURCE,
                    content=self.evidence * 4,
                    retrieved_by="web_researcher",
                ),
                author="web_researcher",
            )
            board.write(
                "final_report",
                FinalReport(
                    answer="The Python 3.13 release notes describe changed behavior.",
                    key_points=[],
                    citations=[
                        Citation(
                            source_type="web",
                            source_ref=SOURCE,
                            quote_or_summary="release notes",
                        )
                    ],
                    confidence=0.9,
                    run_id=self.run_id,
                ),
                author="writer",
            )
        if self.critique:
            board.write(
                "critique",
                Critique(approved=True, severity="low", issues=[]),
                author="critic",
            )
        yield _final_event()


class _Boom:
    def __init__(self, exc: BaseException) -> None:
        self.session_service = _Sessions()
        self.exc = exc

    async def run_async(self, **_kwargs):
        raise self.exc
        yield  # pragma: no cover


def _execute(factory) -> RunSummary:
    return asyncio.run(
        runner_mod._execute(
            QUESTION, "sequential", verbose=False, runner_factory=factory
        )
    )


def test_an_approved_run_ends_in_success() -> None:
    """Regression: finish() used to latch "failure" before the outcome existed."""
    summary = _execute(
        lambda run_id, pattern, recorder: _Runner(
            run_id, evidence=EVIDENCE, critique=True
        )
    )

    assert summary.terminal_state == "success"
    assert summary.outcome == "approved"
    assert summary.failed is False


def test_a_run_without_a_critique_is_a_terminal_failure() -> None:
    summary = _execute(
        lambda run_id, pattern, recorder: _Runner(
            run_id, evidence=EVIDENCE, critique=False
        )
    )

    assert summary.terminal_state == "failure"
    assert summary.critic_status == "missing"


def test_a_timed_out_run_records_timeout_and_keeps_its_summary() -> None:
    summary = _execute(
        lambda run_id, pattern, recorder: _Boom(TimeoutError("stage deadline"))
    )

    assert summary.terminal_state == "timeout"
    assert summary.failed is True
    assert summary.degraded is True
    assert any("timeout" in error for error in summary.stage_errors)


def test_a_cancelled_run_records_cancelled_and_propagates(monkeypatch) -> None:
    recorded: list[RunSummary] = []
    monkeypatch.setattr(
        runner_mod, "_emit_summary", lambda recorder, summary, pattern: recorded.append(summary)
    )

    with pytest.raises(asyncio.CancelledError):
        _execute(lambda run_id, pattern, recorder: _Boom(asyncio.CancelledError()))

    assert len(recorded) == 1
    assert recorded[0].terminal_state == "cancelled"
    assert recorded[0].failed is True


def test_a_failed_run_carries_its_summary_out_of_the_exception() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        _execute(lambda run_id, pattern, recorder: _Boom(RuntimeError("model exploded")))

    summary = runner_mod.carried_summary(excinfo.value)

    assert summary is not None
    assert summary.terminal_state == "failure"
    assert any("model exploded" in error for error in summary.stage_errors)


def test_the_cli_writes_the_terminal_state_of_a_failed_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "server_is_up", lambda *a, **k: True)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(runner_mod, "run_one", boom)
    target = tmp_path / "summary.json"

    code = runner_mod.main([QUESTION, "--pattern", "sequential", "--json-summary", str(target)])

    assert code == 2
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["terminal_state"] == "failure"
    assert "model exploded" in " ".join(payload["stage_errors"])


def test_the_cli_records_an_interrupted_run_as_cancelled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "server_is_up", lambda *a, **k: True)

    async def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_mod, "run_one", interrupted)
    target = tmp_path / "summary.json"

    code = runner_mod.main([QUESTION, "--pattern", "sequential", "--json-summary", str(target)])

    assert code == 2
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["terminal_state"] == "cancelled"


def test_every_run_reports_exactly_one_terminal_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "server_is_up", lambda *a, **k: True)

    async def give_up(*_args, **_kwargs):
        return runner_mod.failed_summary("sequential", "run-x", "cap reached")

    monkeypatch.setattr(runner_mod, "run_one", give_up)
    target = tmp_path / "summary.json"

    assert runner_mod.main([QUESTION, "--pattern", "sequential", "--json-summary", str(target)]) == 2
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["terminal_state"] in TERMINAL_STATES


# --- no orphaned child work -------------------------------------------------


def _collect(agent) -> list:
    async def run() -> list:
        return [event async for event in agent._run_async_impl(None)]

    return asyncio.run(run())


def test_bounded_gather_cancels_survivors_on_timeout() -> None:
    finished: list[str] = []

    async def slow() -> str:
        try:
            await asyncio.sleep(30)
            finished.append("slow")
            return "slow"
        finally:
            finished.append("slow finally")

    async def quick() -> str:
        await asyncio.sleep(0.01)
        return "quick"

    async def scenario() -> None:
        with pytest.raises(StageTimeout):
            await run_bounded([slow(), quick()], timeout=0.1, label="pool")
        leftover = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        assert leftover == [], f"tasks outlived the bounded call: {leftover}"

    asyncio.run(scenario())

    assert finished == ["slow finally"]


def test_bounded_gather_cancels_siblings_when_one_worker_fails() -> None:
    async def slow() -> str:
        await asyncio.sleep(30)
        return "slow"

    async def failing() -> str:
        await asyncio.sleep(0.01)
        raise ValueError("worker died")

    async def scenario() -> None:
        with pytest.raises(ValueError):
            await run_bounded([slow(), failing()], timeout=30, label="pool")
        leftover = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        assert leftover == []

    asyncio.run(scenario())


def test_cancel_tasks_leaves_nothing_pending() -> None:
    async def scenario() -> int:
        tasks = [asyncio.ensure_future(asyncio.sleep(30)) for _ in range(3)]
        return await cancel_tasks(tasks)

    assert asyncio.run(scenario()) == 3


class _Toolset:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Stage:
    """A stage that owns a toolset, and optionally raises or hangs."""

    def __init__(self, name: str, *, exc: BaseException | None = None,
                 hang: float = 0.0) -> None:
        self.name = name
        self.toolset = _Toolset()
        self.tools = [self.toolset]
        self.exc = exc
        self.hang = hang

    async def run_async(self, ctx):
        if self.hang:
            await asyncio.sleep(self.hang)
        if self.exc:
            raise self.exc
        yield None


def test_close_toolsets_closes_nested_and_repeated_toolsets() -> None:
    first, second = _Toolset(), _Toolset()

    class Outer:
        tools = [first]

        class Inner:
            tools = [first, second]

        inner = Inner()

    async def scenario() -> int:
        return await close_toolsets(Outer())

    assert asyncio.run(scenario()) == 2
    assert first.closed is True and second.closed is True


def test_the_supervisor_closes_stage_toolsets_when_a_stage_raises() -> None:
    ok = _Stage("writer")
    broken = _Stage("critic", exc=RuntimeError("critic failed"))
    agent = SupervisorAgent(name="supervisor", run_id="run-f", stages=[ok, broken])

    with pytest.raises(RuntimeError):
        _collect(agent)

    assert ok.toolset.closed is True
    assert broken.toolset.closed is True


def test_the_parallel_pool_closes_every_worker_after_a_timeout(monkeypatch) -> None:
    monkeypatch.setenv("ATHENA_RESEARCH_POOL_TIMEOUT_S", "0.1")
    try:
        hanging = _Stage("web_researcher", hang=30)
        quick = _Stage("rag_researcher")
        pool = ConcurrentWorkers(name="pool", workers=[hanging, quick])

        with pytest.raises(StageTimeout):
            _collect(pool)
    finally:
        monkeypatch.delenv("ATHENA_RESEARCH_POOL_TIMEOUT_S", raising=False)

    assert hanging.toolset.closed is True
    assert quick.toolset.closed is True


def test_execute_closes_the_runners_agent_toolset_on_failure() -> None:
    toolset = _Toolset()

    class Agent:
        tools = [toolset]

    class Failing:
        def __init__(self) -> None:
            self.session_service = _Sessions()
            self.agent = Agent()

        async def run_async(self, **_kwargs):
            raise RuntimeError("model exploded")
            yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        _execute(lambda run_id, pattern, recorder: Failing())

    assert toolset.closed is True, "the runner's toolset must be released in finally"
