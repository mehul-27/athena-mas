"""Offline tests for the pattern registry and the blackboard patterns."""

from __future__ import annotations

import asyncio
import time

import pytest

from athena.agents.supervisor import build_supervisor
from athena.blackboard import (
    Blackboard,
    Critique,
    EvidenceItem,
    Plan,
    PlanStep,
    SQLiteStore,
    Task,
)
from athena.patterns import VALID_PATTERNS, UnknownPatternError, build_pattern
from athena.patterns.hierarchical import HierarchicalAgent
from athena.patterns.parallel import ConcurrentWorkers


class StubStage:
    """Duck-typed ADK agent: a name and an async run_async(ctx)."""

    def __init__(self, name: str, fn=None) -> None:
        self.name = name
        self.fn = fn
        self.calls = 0

    async def run_async(self, ctx):
        self.calls += 1
        if self.fn:
            self.fn(self.calls)
        yield None


class Sleeper:
    def __init__(self, name: str, delay: float, log: list) -> None:
        self.name = name
        self.delay = delay
        self.log = log

    async def run_async(self, ctx):
        self.log.append(("start", self.name))
        await asyncio.sleep(self.delay)
        self.log.append(("end", self.name))
        yield None


class EvidenceWriter:
    def __init__(self, role: str, blackboard: Blackboard, store) -> None:
        self.name = role
        self.role = role
        self.blackboard = blackboard
        self.store = store

    async def run_async(self, ctx):
        self.blackboard.append(
            "evidence",
            EvidenceItem(
                id=f"e-{self.role}",
                source_type="rag",
                source_ref=f"{self.role}#chunk-1",
                content="finding",
                retrieved_by=self.role,
            ),
            author=self.role,
        )
        yield None


def _collect(agent) -> list:
    async def run() -> list:
        return [event async for event in agent._run_async_impl(None)]

    return asyncio.run(run())


def test_unknown_pattern_is_rejected() -> None:
    with pytest.raises(UnknownPatternError) as excinfo:
        build_pattern("turbo", "run-1")
    assert "sequential" in str(excinfo.value)


def test_every_pattern_builds_with_stubs(monkeypatch) -> None:
    from athena.patterns import hierarchical, parallel, p2p, sequential

    stub = lambda *args, **kwargs: StubStage("stub")  # noqa: E731
    for module in (sequential, parallel, hierarchical):
        monkeypatch.setattr(module, "build_planner", stub, raising=False)
        monkeypatch.setattr(module, "build_a2a_stage", stub, raising=False)
    monkeypatch.setattr(sequential, "build_researcher", stub, raising=False)
    monkeypatch.setattr(parallel, "build_research_pool", stub, raising=False)
    monkeypatch.setattr(parallel, "build_role_researcher", stub, raising=False)
    monkeypatch.setattr(hierarchical, "build_role_researcher", stub, raising=False)
    monkeypatch.setattr(p2p, "build_planner", stub, raising=False)
    monkeypatch.setattr(p2p, "build_researcher", stub, raising=False)


    for name in VALID_PATTERNS:
        assert build_pattern(name, "run-stub") is not None


def test_sequential_stage_order_is_unchanged(monkeypatch) -> None:
    from athena.patterns import sequential

    monkeypatch.setattr(sequential, "build_planner", lambda *a, **k: StubStage("planner"))
    monkeypatch.setattr(sequential, "build_researcher", lambda *a, **k: StubStage("researcher"))
    monkeypatch.setattr(
        sequential, "build_a2a_stage", lambda role, *a, **k: StubStage(role)
    )

    supervisor = build_supervisor("run-order", pattern="sequential")

    assert [stage.name for stage in supervisor.stages] == [
        "planner",
        "researcher",
        "analyst",
        "writer",
        "critic",
    ]


def test_parallel_workers_overlap() -> None:
    log: list = []
    pool = ConcurrentWorkers(
        name="pool", workers=[Sleeper(f"w{index}", 0.25, log) for index in range(3)]
    )

    started = time.perf_counter()
    _collect(pool)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.6, f"workers ran in sequence ({elapsed:.2f}s for 3 x 0.25s)"
    assert [entry for entry in log if entry[0] == "start"] == [
        ("start", "w0"),
        ("start", "w1"),
        ("start", "w2"),
    ]


def test_parallel_evidence_append_is_safe(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=store)
    roles = ("rag_researcher", "web_researcher", "calc_researcher")
    pool = ConcurrentWorkers(
        name="pool",
        workers=[EvidenceWriter(role, blackboard, store) for role in roles],
    )

    _collect(pool)

    items, version = blackboard.read("evidence")
    assert len(items) == 3
    assert version == 3
    assert {item.retrieved_by for item in items} == set(roles)
    assert {entry["author"] for entry in blackboard.history()} >= set(roles)


def _hierarchical(store, blackboard, writer_calls, critic_plan):
    def write_plan(_=None):
        blackboard.write(
            "plan",
            Plan(
                steps=[
                    PlanStep(id="s1", description="search", assigned_to="rag_researcher"),
                    PlanStep(id="s2", description="compute", assigned_to="calc_researcher"),
                ]
            ),
            author="planner",
        )

    def write_report(_=None):
        writer_calls.append(1)

    def write_critique(call_number):
        approved = critic_plan[call_number - 1] if call_number <= len(critic_plan) else True
        blackboard.write(
            "critique",
            Critique(
                issues=[] if approved else ["answer is verbose and poorly organized"],
                severity="low" if approved else "high",
                approved=approved,
            ),
            author="critic",
        )

    workers = {
        role: StubStage(role) for role in ("rag_researcher", "web_researcher", "calc_researcher")
    }
    agent = HierarchicalAgent(
        name="supervisor",
        planner=StubStage("planner", write_plan),
        workers=workers,
        analyst=StubStage("analyst"),
        writer=StubStage("writer", write_report),
        critic=StubStage("critic", write_critique),
        run_id=blackboard.run_id,
        store=store,
        revision_limit=1,
    )
    return agent, workers


def test_hierarchical_dispatches_from_the_plan(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=store)
    agent, workers = _hierarchical(store, blackboard, [], [True])

    _collect(agent)

    assert workers["rag_researcher"].calls == 1
    assert workers["calc_researcher"].calls == 1
    assert workers["web_researcher"].calls == 0


def test_hierarchical_rejection_requests_bounded_research_recovery(
    tmp_path, monkeypatch
) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(
        Task(question="Explain three key changes in Python 3.13."), store=store
    )
    writer_calls: list[int] = []
    recovery_calls: list[int] = []

    def write_plan(_=None):
        blackboard.write(
            "plan",
            Plan(steps=[PlanStep(id="s1", description="search", assigned_to="web_researcher")]),
            author="planner",
        )

    def write_report(call_number):
        writer_calls.append(call_number)

    def write_critique(call_number):
        rejected = call_number == 1
        blackboard.write(
            "critique",
            Critique(
                issues=["missing source evidence for the requested findings"] if rejected else [],
                severity="high" if rejected else "low",
                approved=not rejected,
            ),
            author="critic",
        )

    def fake_researcher(role, run_id, store, task_text=None):
        assert role == "web_researcher"
        assert "CRITIC GAPS" in task_text
        return StubStage("recovery", lambda number: recovery_calls.append(number))

    monkeypatch.setattr(
        "athena.patterns.hierarchical.build_role_researcher", fake_researcher
    )

    agent = HierarchicalAgent(
        name="supervisor",
        planner=StubStage("planner", write_plan),
        workers={
            role: StubStage(role)
            for role in ("rag_researcher", "web_researcher", "calc_researcher")
        },
        analyst=StubStage("analyst"),
        writer=StubStage("writer", write_report),
        critic=StubStage("critic", write_critique),
        run_id=blackboard.run_id,
        store=store,
        revision_limit=1,
    )

    _collect(agent)

    assert len(writer_calls) == 2
    assert recovery_calls == [1]
    assert blackboard.read("critique")[0].approved is True



def test_hierarchical_revises_once_when_rejected(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=store)
    writer_calls: list = []
    agent, _ = _hierarchical(store, blackboard, writer_calls, [False, True])

    events = _collect(agent)

    assert len(writer_calls) == 2
    rounds = [
        event.custom_metadata["revision_rounds"]
        for event in events
        if getattr(event, "custom_metadata", None)
        and "revision_rounds" in event.custom_metadata
    ]
    assert rounds == [1]


def test_hierarchical_stops_at_the_revision_limit(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=store)
    writer_calls: list = []
    agent, _ = _hierarchical(store, blackboard, writer_calls, [False, False, False])

    events = _collect(agent)

    assert len(writer_calls) == 2  # first pass + one revision, then stop
    rounds = [
        event.custom_metadata["revision_rounds"]
        for event in events
        if getattr(event, "custom_metadata", None)
        and "revision_rounds" in event.custom_metadata
    ]
    assert rounds == [1]
