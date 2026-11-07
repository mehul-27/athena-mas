"""Research tool selection, enforcement, plan validation and evidence gates."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import athena.api.app as app_module
from athena.agents import researcher as researcher_module
from athena.agents.planner import prepare_plan
from athena.agents.researcher import AssignedWorker
from athena.agents.routing import ensure_step_texts
from athena.agents.sufficiency import assess
from athena.agents.tool_report import build_tool_report, format_tool_status
from athena.api.app import create_app
from athena.api.runs import RunRegistry
from athena.blackboard import Blackboard, Critique, EvidenceItem, Plan, PlanStep, SQLiteStore, Task
from athena.observability.events import Event
from athena.observability.store import SQLiteEventStore
from athena.patterns import hierarchical as hierarchical_module
from athena.patterns.hierarchical import HierarchicalAgent
from athena.tools import registry

ALL = ["rag_search", "web_search", "fetch_page", "calculate", "forecast"]
PYTHON_URL = "https://docs.python.org/3.13/whatsnew/3.13.html"
PYTHON_QUESTION = f"Read {PYTHON_URL} and summarise the release changes."


class FakeRegistry:
    def __init__(self, active=None):
        self.active = active
        self.started: list[tuple] = []

    def start(self, question, pattern, tools=None):
        self.started.append((question, pattern, tools))
        return "run-1"

    def refresh(self):
        return self.active


class FakeStore:
    def __init__(self, events=None):
        self._events = events or []

    def events(self, _run_id):
        return list(self._events)


class StubStage:
    def __init__(self, name, fn=None):
        self.name = name
        self.fn = fn
        self.calls = 0

    async def run_async(self, ctx):
        self.calls += 1
        if self.fn:
            self.fn(self.calls)
        yield None


class ManualWorker:
    def __init__(self):
        self.calls = 0

    async def run_async(self, ctx):
        self.calls += 1
        yield None


def _collect(agent) -> list:
    async def run():
        return [event async for event in agent._run_async_impl(None)]

    return asyncio.run(run())


def _client(registry, monkeypatch) -> TestClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    return TestClient(create_app(registry))


# ---------------------------------------------------------------- registry


def test_selection_normalisation_distinguishes_all_and_none() -> None:
    assert registry.normalise_selection(None) is None
    assert registry.normalise_selection([]) == ()
    assert registry.normalise_selection(["web_search", "rag_search", "web_search"]) == (
        "rag_search",
        "web_search",
    )
    assert registry.enabled_ids(None) == tuple(ALL)
    assert registry.enabled_ids([]) == ()


def test_unknown_tool_is_rejected() -> None:
    with pytest.raises(registry.UnknownToolError) as excinfo:
        registry.normalise_selection(["web_search", "rm_rf"])
    assert "rm_rf" in str(excinfo.value)


def test_role_and_server_filters_follow_the_selection() -> None:
    selected = ["fetch_page", "rag_search"]

    assert registry.role_tools("web_researcher", selected) == ("fetch_page",)
    assert registry.role_tools("calc_researcher", selected) == ()
    assert registry.available_roles(selected) == ("rag_researcher", "web_researcher")
    assert registry.server_filters(selected) == {
        "rag": ["rag_search"],
        "web": ["fetch_page"],
    }


# ---------------------------------------------------------------- API


def test_api_rejects_unknown_tool_without_starting(monkeypatch) -> None:
    registry_stub = FakeRegistry()
    client = _client(registry_stub, monkeypatch)

    response = client.post(
        "/api/runs",
        json={"question": "q", "pattern": "sequential", "tools": ["rm_rf"]},
    )

    assert response.status_code == 422
    assert "rm_rf" in response.json()["detail"]
    assert registry_stub.started == []


def test_api_passes_the_normalised_selection_to_the_registry(monkeypatch) -> None:
    registry_stub = FakeRegistry()
    client = _client(registry_stub, monkeypatch)

    response = client.post(
        "/api/runs",
        json={
            "question": "q",
            "pattern": "parallel",
            "tools": ["web_search", "fetch_page"],
        },
    )

    assert response.status_code == 200
    assert registry_stub.started == [("q", "parallel", ["web_search", "fetch_page"])]


def test_api_distinguishes_null_tools_from_an_empty_selection(monkeypatch) -> None:
    registry_stub = FakeRegistry()
    client = _client(registry_stub, monkeypatch)

    client.post("/api/runs", json={"question": "q", "pattern": "sequential"})
    client.post(
        "/api/runs",
        json={"question": "q", "pattern": "sequential", "tools": None},
    )
    client.post(
        "/api/runs",
        json={"question": "q", "pattern": "sequential", "tools": []},
    )

    assert registry_stub.started == [
        ("q", "sequential", None),
        ("q", "sequential", None),
        ("q", "sequential", []),
    ]


def test_cli_rejects_unknown_tools_before_starting() -> None:
    from athena.run import main as run_main

    with pytest.raises(SystemExit) as excinfo:
        run_main(["q", "--tools", "rm_rf"])

    assert "rm_rf" in str(excinfo.value)


def test_run_registry_argv_carries_the_exact_selection() -> None:
    runner = RunRegistry(command=["stub"])

    assert "--tools" not in runner._argv("q", "sequential", "r", None)
    assert runner._argv("q", "sequential", "r", ["web_search"])[-2:] == [
        "--tools",
        "web_search",
    ]
    assert runner._argv("q", "sequential", "r", [])[-2:] == ["--tools", ""]


# ---------------------------------------------------------------- plan policy


def test_plan_filters_disabled_roles_and_records_the_block(tmp_path) -> None:
    policy = prepare_plan(
        [
            "web_researcher: fetch the official release page",
            "rag_researcher: search the supplied handbook",
        ],
        "According to the supplied handbook, what is configuration management?",
        ["rag_search"],
    )

    assert [step.assigned_to for step in policy.steps] == ["rag_researcher"]
    assert any("web_researcher" in item for item in policy.blocked)
    assert any("cannot execute" in item for item in policy.limitations)


def test_required_web_step_is_not_inserted_when_web_is_disabled() -> None:
    steps = ensure_step_texts(
        [],
        "What changed in Python 3.13?",
        ["rag_search"],
    )
    assert not any(step.startswith("web_researcher:") for step in steps)


def test_planner_prompt_lists_enabled_and_disabled_tools(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="q", tools=["rag_search"]), store=store)
    from athena.agents.planner import build_planner

    agent = build_planner(board.run_id, store)
    assert "ENABLED RESEARCH TOOLS: rag_search" in agent.instruction
    assert "DISABLED RESEARCH TOOLS" in agent.instruction
    assert "fetch_page" in agent.instruction


# ---------------------------------------------------------------- tool exposure


def test_researcher_toolsets_are_filtered_before_adk_sees_them(monkeypatch) -> None:
    captured: list[tuple] = []

    def fake_build(module, timeout=120.0, tools=None, **_health):
        captured.append((module, tuple(tools or ())))
        return module

    monkeypatch.setattr(researcher_module, "build_toolset", fake_build)

    assert researcher_module._toolsets(()) == []
    assert researcher_module._toolsets(("fetch_page",)) == ["athena.tools.web_server"]
    assert captured == [("athena.tools.web_server", ("fetch_page",))]


def test_selection_guard_refuses_a_tool_outside_the_selection() -> None:
    guard = researcher_module.selection_guard({"rag_search"}, "researcher")

    blocked = guard(SimpleNamespace(name="web_search"), {}, None)
    assert blocked["isError"] is True
    assert "not enabled" in blocked["error"]
    assert guard(SimpleNamespace(name="rag_search"), {}, None) is None


def test_assigned_worker_does_not_dispatch_a_disabled_role(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="q", tools=["rag_search"]), store=store)
    board.write(
        "plan",
        Plan(steps=[PlanStep(id="s1", description="web", assigned_to="web_researcher")]),
        author="planner",
    )
    worker = ManualWorker()
    assigned = AssignedWorker(
        name="web_researcher",
        role="web_researcher",
        worker=worker,
        run_id=board.run_id,
        store=store,
    )

    events = _collect(assigned)

    assert worker.calls == 0
    assert events[0].custom_metadata["skipped"] is True
    assert "tools disabled" in events[0].custom_metadata["reason"]


def test_missing_plan_falls_back_only_to_enabled_workers(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(
        Task(
            question="According to the supplied handbook, what is configuration management?",
            tools=["rag_search"],
        ),
        store=store,
    )
    rag = ManualWorker()
    web = ManualWorker()

    def assigned(role, worker):
        return AssignedWorker(
            name=role, role=role, worker=worker, run_id=board.run_id, store=store
        )

    _collect(assigned("rag_researcher", rag))
    _collect(assigned("web_researcher", web))

    assert rag.calls == 1, "the enabled RAG worker must run when the planner wrote no plan"
    assert web.calls == 0, "a disabled web worker must not be used as a fallback"


def test_hierarchical_skips_a_disabled_worker_but_runs_an_enabled_one(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="q", tools=["rag_search"]), store=store)

    def write_plan(_=None):
        board.write(
            "plan",
            Plan(steps=[
                PlanStep(id="s1", description="web", assigned_to="web_researcher"),
                PlanStep(id="s2", description="rag", assigned_to="rag_researcher"),
            ]),
            author="planner",
        )

    def approve(_=None):
        board.write(
            "critique",
            Critique(issues=[], severity="low", approved=True),
            author="critic",
        )

    workers = {
        "rag_researcher": StubStage("rag_researcher"),
        "web_researcher": StubStage("web_researcher"),
        "calc_researcher": StubStage("calc_researcher"),
    }
    agent = HierarchicalAgent(
        name="supervisor",
        planner=StubStage("planner", write_plan),
        workers=workers,
        analyst=StubStage("analyst"),
        writer=StubStage("writer"),
        critic=StubStage("critic", approve),
        run_id=board.run_id,
        store=store,
        revision_limit=1,
    )

    events = _collect(agent)

    assert workers["rag_researcher"].calls == 1
    assert workers["web_researcher"].calls == 0
    assert any(
        isinstance(getattr(event, "custom_metadata", None), dict)
        and event.custom_metadata.get("skipped")
        and event.custom_metadata.get("role") == "web_researcher"
        for event in events
    )


def test_hierarchical_recovery_never_re_enables_a_disabled_tool(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="What changed in Python 3.13?", tools=["rag_search"]), store=store)
    recovery_calls: list = []

    def write_plan(_=None):
        board.write(
            "plan",
            Plan(steps=[PlanStep(id="s1", description="web", assigned_to="web_researcher")]),
            author="planner",
        )

    def reject(_=None):
        board.write(
            "critique",
            Critique(
                issues=["missing source evidence for the requested findings"],
                severity="high",
                approved=False,
            ),
            author="critic",
        )

    def fake_researcher(*args, **kwargs):
        recovery_calls.append(args)
        return StubStage("recovery")

    monkeypatch.setattr(hierarchical_module, "build_role_researcher", fake_researcher)
    agent = HierarchicalAgent(
        name="supervisor",
        planner=StubStage("planner", write_plan),
        workers={role: StubStage(role) for role in ("rag_researcher", "web_researcher", "calc_researcher")},
        analyst=StubStage("analyst"),
        writer=StubStage("writer"),
        critic=StubStage("critic", reject),
        run_id=board.run_id,
        store=store,
        revision_limit=1,
    )

    _collect(agent)

    assert recovery_calls == []


# ---------------------------------------------------------------- sufficiency


def _evidence(evidence_id: str, source_type: str, source_ref: str, content: str):
    return EvidenceItem(
        id=evidence_id,
        source_type=source_type,
        source_ref=source_ref,
        content=content,
        retrieved_by="researcher",
    )


def test_explicit_url_requires_a_successful_fetch() -> None:
    assessment = assess(
        PYTHON_QUESTION,
        [_evidence("doc-1", "rag", "handbook.pdf#p1", "unrelated configuration management " * 8)],
        enabled_tools=ALL,
    )

    assert assessment.ok is False
    assert any(PYTHON_URL in issue for issue in assessment.issues)
    assert any("no successful fetch" in issue for issue in assessment.issues)


def test_explicit_url_is_satisfied_by_the_fetched_page() -> None:
    item = _evidence(
        "fetch-1",
        "web",
        PYTHON_URL,
        "Python 3.13 release changes include a new interactive interpreter. " * 6,
    )
    assessment = assess(PYTHON_QUESTION, [item], enabled_tools=ALL)

    assert assessment.ok is True
    assert assessment.required_urls == (PYTHON_URL,)


def test_fetch_disabled_is_reported_as_an_unmet_capability() -> None:
    item = _evidence(
        "fetch-1",
        "web",
        PYTHON_URL,
        "Python 3.13 release changes include a new interactive interpreter. " * 6,
    )
    assessment = assess(PYTHON_QUESTION, [item], enabled_tools=["web_search"])

    assert assessment.ok is False
    assert "fetch_page" in assessment.missing_tools
    assert any("Fetch page is disabled" in issue for issue in assessment.issues)


def test_search_snippets_are_labelled_as_search_only() -> None:
    snippet = _evidence(
        "web-1",
        "web",
        PYTHON_URL,
        "Python 3.13 release notes and the changed features. " * 6,
    )
    assessment = assess("What changed in Python 3.13?", [snippet], enabled_tools=ALL)

    assert assessment.search_only is True
    assert any("Only search-result snippets" in issue for issue in assessment.issues)


def test_url_plus_snippet_does_not_count_as_a_fetched_page() -> None:
    snippet = _evidence(
        "web-1",
        "web",
        PYTHON_URL,
        "Python 3.13 release changes include a new interpreter. " * 6,
    )
    assessment = assess(PYTHON_QUESTION, [snippet], enabled_tools=ALL)

    assert assessment.search_only is True
    assert any("no successful fetch" in issue for issue in assessment.issues)
    assert any("Only search-result snippets" in issue for issue in assessment.issues)


def test_a_successful_fetch_of_the_wrong_url_does_not_satisfy_the_requirement() -> None:
    wrong = _evidence(
        "fetch-1",
        "web",
        "https://example.com/a-different-page",
        "Python 3.13 release changes include a new interpreter. " * 6,
    )
    assessment = assess(PYTHON_QUESTION, [wrong], enabled_tools=ALL)

    assert assessment.ok is False
    assert any(PYTHON_URL in issue for issue in assessment.issues)
    assert assessment.required_urls == (PYTHON_URL,)


def test_no_tools_is_never_treated_as_sufficient() -> None:
    assessment = assess("What is systems engineering?", [], enabled_tools=[])

    assert assessment.ok is False
    assert any("No research tools are enabled" in issue for issue in assessment.issues)


def test_disabled_web_tools_are_an_unmet_requirement() -> None:
    assessment = assess(
        "What changed in Python 3.13?",
        [],
        enabled_tools=["calculate"],
    )

    assert assessment.ok is False
    assert "web_search" in assessment.missing_tools
    assert any("Web research is disabled" in issue for issue in assessment.issues)


def test_failed_fetch_is_surfaced_by_the_tool_report() -> None:
    assessment = assess(
        PYTHON_QUESTION,
        [],
        enabled_tools=ALL,
        tool_report={"failed": ["fetch_page"]},
    )

    assert assessment.ok is False
    assert any("fetch_page failed" in issue for issue in assessment.issues)


def test_failed_search_is_a_limitation_not_a_global_evidence_failure() -> None:
    assessment = assess(
        "What changed in Python 3.13?",
        [],
        enabled_tools=ALL,
        tool_report={"failed": ["web_search"]},
    )

    assert assessment.ok is False
    assert any("No relevant fetched web pages" in issue for issue in assessment.issues)
    assert any("web_search" in item for item in assessment.limitations)
    assert not any("current external evidence was not collected" in issue
                   for issue in assessment.issues)


def test_disabled_rag_is_reported_for_a_local_document_question() -> None:
    question = "According to the supplied handbook, what is configuration management?"
    assessment = assess(question, [], enabled_tools=["web_search"])

    assert assessment.ok is False
    assert "rag_search" in assessment.missing_tools
    assert any("Local document search is disabled" in issue for issue in assessment.issues)


def test_empty_or_irrelevant_local_retrieval_is_insufficient() -> None:
    question = "According to the supplied handbook, what is configuration management?"
    irrelevant = _evidence(
        "doc-1",
        "rag",
        "handbook.pdf#p1-chunk-1",
        "unrelated material about orbital mechanics " * 8,
    )

    empty = assess(question, [], enabled_tools=["rag_search"])
    irrelevant_assessment = assess(
        question, [irrelevant], enabled_tools=["rag_search"]
    )

    assert empty.ok is False
    assert irrelevant_assessment.ok is False
    assert any(
        "No relevant local-document evidence" in issue
        for issue in irrelevant_assessment.issues
    )


# ---------------------------------------------------------------- tool report


def test_tool_report_counts_only_real_invocations(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(
        Task(question="q", tools=["web_search", "fetch_page"]), store=store
    )
    board.write(
        "plan",
        Plan(
            steps=[PlanStep(id="s1", description="web", assigned_to="web_researcher")],
            blocked=["rag_researcher: unavailable"],
        ),
        author="planner",
    )
    event_store = SQLiteEventStore(tmp_path / "events.db")
    event_store.emit(Event(
        run_id=board.run_id, kind="tool_call", agent="web_researcher",
        framework="adk", ok=True, payload_summary="web_search: query",
    ))
    event_store.emit(Event(
        run_id=board.run_id, kind="tool_call", agent="web_researcher",
        framework="adk", ok=False, payload_summary="fetch_page: url",
        error_type="ToolError", error_message="blocked",
    ))
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events.db"))

    report = build_tool_report(board.run_id, store)

    assert report["enabled"] == ["web_search", "fetch_page"]
    assert report["planned"] == ["web_search", "fetch_page"]
    assert report["invoked"] == ["fetch_page", "web_search"]
    assert report["succeeded"] == ["web_search"]
    assert report["failed"] == ["fetch_page"]
    assert "rag_researcher: unavailable" in report["skipped"]
    text = format_tool_status(report)
    assert "Tools actually attempted" in text
    assert "fetch_page (web_researcher): blocked" in text


# ---------------------------------------------------------------- endpoints


def test_tool_report_keeps_planned_but_disabled_tools_visible(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="q", tools=["rag_search"]), store=store)
    board.write(
        "plan",
        Plan(
            steps=[PlanStep(id="s1", description="web", assigned_to="web_researcher")],
            blocked=["web_researcher: fetch the release page"],
        ),
        author="planner",
    )

    report = build_tool_report(board.run_id, store)

    assert report["enabled"] == ["rag_search"]
    assert report["planned"] == ["web_search", "fetch_page"]
    assert report["planned_blocked"] == ["web_search", "fetch_page"]
    assert report["invoked"] == []
    assert "web_researcher: fetch the release page" in report["skipped"]
    assert "Planned tools disabled by the user" in format_tool_status(report)


def test_calc_worker_receives_only_calc_tools(monkeypatch, tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="q", tools=ALL), store=store)
    captured: list[tuple] = []

    def fake_build(module, timeout=120.0, tools=None, **_health):
        captured.append((module, tuple(tools or ())))
        return module

    monkeypatch.setattr(researcher_module, "build_toolset", fake_build)
    monkeypatch.setattr(
        researcher_module,
        "LlmAgent",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    researcher_module.build_role_researcher("calc_researcher", board.run_id, store)

    assert captured == [("athena.tools.calc_server", ("calculate", "forecast"))]


def test_selection_guard_distinguishes_worker_assignment_from_user_selection() -> None:
    worker_guard = researcher_module.selection_guard(
        {"calculate"}, "calc_researcher", {"web_search", "calculate"}
    )
    blocked = worker_guard(SimpleNamespace(name="web_search"), {"query": "x"}, None)

    assert blocked["isError"] is True
    assert blocked["error_type"] == "ToolNotAssigned"
    assert blocked["blocked_by"] == "worker_assignment"
    assert "not assigned to calc_researcher" in blocked["error"]
    assert "not enabled for this run" not in blocked["error"]

    user_guard = researcher_module.selection_guard(
        {"calculate"}, "calc_researcher", {"calculate"}
    )
    disabled = user_guard(SimpleNamespace(name="web_search"), {"query": "x"}, None)
    assert disabled["error_type"] == "ToolDisabled"
    assert disabled["blocked_by"] == "user_selection"
    assert "not enabled for this run" in disabled["error"]


def test_tool_report_preserves_mixed_invocations_and_block_reasons(
    tmp_path, monkeypatch
) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="q", tools=ALL), store=store)
    board.write(
        "plan",
        Plan(
            steps=[PlanStep(id="s1", description="web search and fetch", assigned_to="web_researcher")],
        ),
        author="planner",
    )
    event_store = SQLiteEventStore(tmp_path / "events.db")
    events = [
        Event(
            run_id=board.run_id, kind="tool_call", agent="web_researcher",
            framework="adk", ok=True,
            payload_summary="fetch_page: {'url': 'https://docs.example/a'}",
            extra={
                "result_status": "ok", "requested_url": "https://docs.example/a",
                "sources": [{"url": "https://docs.example/a", "status": "ok"}],
            },
        ),
        Event(
            run_id=board.run_id, kind="tool_call", agent="web_researcher",
            framework="adk", ok=False, error_type="ToolError", error_message="HTTP 404",
            payload_summary="fetch_page: {'url': 'https://docs.example/b'}",
        ),
        Event(
            run_id=board.run_id, kind="tool_call", agent="web_researcher",
            framework="adk", ok=False, error_type="ToolError",
            error_message="provider throttled", payload_summary="web_search: {'query': 'q'}",
        ),
        Event(
            run_id=board.run_id, kind="tool_call", agent="calc_researcher",
            framework="adk", ok=False, error_type="ToolNotAssigned",
            error_message="Blocked: web_search is not assigned to calc_researcher.",
            payload_summary="web_search: {'query': 'q'}",
            extra={"blocked_by": "worker_assignment"},
        ),
    ]
    for event in events:
        event_store.emit(event)
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events.db"))

    report = build_tool_report(board.run_id, store)

    assert report["invoked"] == ["fetch_page", "web_search"]
    assert report["mixed"] == ["fetch_page"]
    assert report["usable_evidence"] == ["fetch_page"]
    assert report["blocked"]["worker_assignment"] == ["web_search"]
    outcomes = {(call["seq"], call["tool"]): call["outcome"] for call in report["calls"]}
    assert outcomes[(1, "fetch_page")] == "succeeded_with_evidence"
    assert outcomes[(2, "fetch_page")] == "failed"
    assert outcomes[(3, "web_search")] == "failed"
    assert outcomes[(4, "web_search")] == "blocked_worker_assignment"
    assert report["blocked_counts"]["worker_assignment"] == 1
    text = format_tool_status(report)
    assert "Blocked by worker assignment (not a user restriction): web_search" in text
    assert "Blocked by user selection" not in text


def test_tool_report_collapses_old_synthetic_block_event(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="q", tools=ALL), store=store)
    board.write(
        "plan",
        Plan(steps=[PlanStep(id="s1", description="compute numbers", assigned_to="calc_researcher")]),
        author="planner",
    )
    event_store = SQLiteEventStore(tmp_path / "events.db")
    event_store.emit(Event(
        run_id=board.run_id, kind="tool_call", agent="calc_researcher",
        framework="adk", ok=False, error_type="ToolDisabled",
        error_message="web_search is not enabled for this run.",
        payload_summary="web_search: blocked by tool selection",
    ))
    event_store.emit(Event(
        run_id=board.run_id, kind="tool_call", agent="calc_researcher",
        framework="adk", ok=False, error_type="ToolError",
        error_message="web_search is not enabled for this run.",
        payload_summary="web_search: {'query': 'q'}",
    ))
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events.db"))

    report = build_tool_report(board.run_id, store)

    assert report["call_count"] == 1
    assert report["telemetry_duplicates_removed"] == 1
    assert report["calls"][0]["blocked_by"] == "worker_assignment"
    assert report["calls"][0]["outcome"] == "blocked_worker_assignment"


def test_mcp_tool_environment_carries_search_fallback_without_model_secrets(
    monkeypatch,
) -> None:
    from athena.agents import researcher

    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "searxng")
    monkeypatch.setenv("SEARCH_FALLBACK_ENDPOINT", "http://127.0.0.1:8080")
    monkeypatch.setenv("LLM_API_KEY", "sk-must-not-cross-the-mcp-boundary")

    env = researcher.mcp_environment()

    assert env["SEARCH_FALLBACK_PROVIDER"] == "searxng"
    assert env["SEARCH_FALLBACK_ENDPOINT"] == "http://127.0.0.1:8080"
    assert "LLM_API_KEY" not in env


def test_tool_report_classifies_duplicate_calls_separately_from_budget(
    tmp_path, monkeypatch
) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="q", tools=["web_search"]), store=store)
    event_store = SQLiteEventStore(tmp_path / "events.db")
    event_store.emit(Event(
        run_id=board.run_id, kind="tool_call", agent="web_researcher",
        framework="adk", ok=False, error_type="ToolDuplicateBlocked",
        error_message="Blocked: this tool call was already made.",
        payload_summary="web_search: {'query': 'q'}",
        extra={"blocked_by": "duplicate"},
    ))
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events.db"))

    report = build_tool_report(board.run_id, store)

    assert report["blocked"]["duplicate"] == ["web_search"]
    assert report["blocked"]["budget"] == []
    assert report["calls"][0]["outcome"] == "blocked_duplicate"
    assert "Blocked as duplicate: web_search" in format_tool_status(report)
    assert "Blocked by worker budget" not in format_tool_status(report)


def test_tool_report_does_not_infer_forecast_from_a_calculation_step(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="q", tools=ALL), store=store)
    board.write(
        "plan",
        Plan(steps=[
            PlanStep(id="s1", description="compute the working numbers", assigned_to="calc_researcher")
        ]),
        author="planner",
    )

    report = build_tool_report(board.run_id, store)

    assert report["planned"] == ["calculate"]
    assert "forecast" not in report["planned"]
    assert report["assignments"]["calc_researcher"] == ["calculate", "forecast"]


def test_tool_report_flags_misattributed_fetch_telemetry(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="q", tools=ALL), store=store)
    board.write(
        "plan",
        Plan(steps=[PlanStep(id="s1", description="fetch pages", assigned_to="web_researcher")]),
        author="planner",
    )
    event_store = SQLiteEventStore(tmp_path / "events.db")
    event_store.emit(Event(
        run_id=board.run_id, kind="tool_call", agent="web_researcher",
        framework="adk", ok=True,
        payload_summary="fetch_page: {'url': 'https://www.spacex.com/manual.pdf'}",
        extra={"sources": [{"url": "https://example.com/article", "status": "ok"}]},
    ))
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events.db"))

    report = build_tool_report(board.run_id, store)

    call = report["calls"][0]
    assert call["telemetry_conflict"] is True
    assert call["outcome"] == "telemetry_conflict"
    assert call["usable_evidence"] is False
    assert len(report["telemetry_conflicts"]) == 1


def test_snapshot_evidence_keeps_source_type_and_content(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BLACKBOARD_DB", str(tmp_path / "bb.db"))
    store = SQLiteStore(tmp_path / "bb.db")
    board = Blackboard.create(Task(question="q"), store=store)
    board.append(
        "evidence",
        EvidenceItem(
            id="doc_1",
            source_type="rag",
            source_ref="handbook.pdf#p7-chunk-3",
            content="The recorded passage",
            score=0.8,
            retrieved_by="rag_researcher",
        ),
        author="rag_researcher",
    )
    client = _client(FakeRegistry(), monkeypatch)

    response = client.get(f"/api/runs/{board.run_id}")

    assert response.status_code == 200
    item = response.json()["evidence"][0]
    assert item["source_type"] == "rag"
    assert item["text"] == "The recorded passage"
    assert item["content"] == "The recorded passage"


def test_missing_run_snapshot_is_404(monkeypatch) -> None:
    client = _client(FakeRegistry(), monkeypatch)

    response = client.get("/api/runs/not-a-run")

    assert response.status_code == 404


def test_events_endpoint_restores_a_trace(monkeypatch) -> None:
    event = Event(
        run_id="r1", seq=1, kind="stage_start", agent="planner", framework="adk",
    )
    monkeypatch.setattr(app_module, "_store", lambda: FakeStore([event]))
    client = _client(FakeRegistry(), monkeypatch)

    response = client.get("/api/runs/r1/events")

    assert response.status_code == 200
    assert response.json()["run_id"] == "r1"
    assert response.json()["events"][0]["agent"] == "planner"


def test_research_tool_catalogue_exposes_the_registry(monkeypatch) -> None:
    monkeypatch.setattr("athena.api.activation.active_env", lambda: {})
    client = _client(FakeRegistry(), monkeypatch)

    response = client.get("/api/research/tools")

    assert response.status_code == 200
    ids = [tool["id"] for tool in response.json()["tools"]]
    assert ids == ALL
    assert response.json()["all_tools"] == ALL
