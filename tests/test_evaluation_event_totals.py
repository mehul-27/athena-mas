"""Evaluation token aggregation over the real monitoring event store.

CrewAI writes each role's usage twice: once as its own ``model_call`` and once
mirrored in the ADK caller's ``a2a_call`` acknowledgement. ``event_totals``
must prefer the server's per-agent usage and use the acknowledgement only when
that agent recorded no model usage at all.

These tests put a fake store *below* ``event_totals`` and pass real ``Event``
objects through it, so the aggregation function itself is never stubbed.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from athena.observability import store as store_module  # noqa: E402
from athena.observability.events import Event  # noqa: E402
from evaluation import runner  # noqa: E402

RUN_ID = "run-accounting"


class FakeEventStore:
    def __init__(self, events: list[Event]) -> None:
        self._events = events
        self.requested: list[str] = []

    def events(self, run_id: str) -> list[Event]:
        self.requested.append(run_id)
        return list(self._events)


def use_events(monkeypatch, events: list[Event]) -> FakeEventStore:
    fake = FakeEventStore(events)

    def from_env(cls):
        return fake

    monkeypatch.setattr(store_module.SQLiteEventStore, "from_env", classmethod(from_env))
    return fake


def make_event(
    kind: str,
    agent: str,
    *,
    framework: str = "crewai",
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    reasoning_tokens: int | None = None,
    **fields,
) -> Event:
    extra = dict(fields.pop("extra", {}))
    if reasoning_tokens is not None:
        extra["reasoning_tokens"] = reasoning_tokens
    return Event(
        run_id=fields.pop("run_id", RUN_ID),
        kind=kind,
        agent=agent,
        framework=framework,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        extra=extra,
        **fields,
    )


def test_mirrored_crewai_usage_is_counted_once(monkeypatch):
    fake = use_events(
        monkeypatch,
        [
            make_event(
                "model_call", "writer",
                tokens_in=800, tokens_out=200, reasoning_tokens=30,
            ),
            make_event(
                "a2a_call", "writer", framework="adk",
                tokens_in=800, tokens_out=200, reasoning_tokens=30,
                extra={"calls": 1, "handoff": "adk->crewai", "role": "writer"},
            ),
        ],
    )

    totals = runner.event_totals(RUN_ID)

    assert fake.requested == [RUN_ID]
    assert totals == {
        "tokens_in": 800,
        "tokens_out": 200,
        "reasoning_tokens": 30,
        "model_calls": 1,
        "tool_calls": 0,
        "recovery_events": 0,
        "errors": 0,
    }


def test_ack_usage_is_the_fallback_for_an_ack_only_role(monkeypatch):
    use_events(
        monkeypatch,
        [
            make_event(
                "a2a_call", "analyst", framework="adk",
                tokens_in=400, tokens_out=120, reasoning_tokens=12,
                extra={"calls": 1, "handoff": "adk->crewai", "role": "analyst"},
            ),
        ],
    )

    totals = runner.event_totals(RUN_ID)

    assert totals["tokens_in"] == 400
    assert totals["tokens_out"] == 120
    assert totals["reasoning_tokens"] == 12
    # An ack is not a model call; the count stays a count of model_call events.
    assert totals["model_calls"] == 0


def test_zero_model_usage_falls_back_to_the_ack(monkeypatch):
    use_events(
        monkeypatch,
        [
            make_event(
                "model_call", "critic",
                tokens_in=0, tokens_out=0, reasoning_tokens=0,
            ),
            make_event(
                "a2a_call", "critic", framework="adk",
                tokens_in=90, tokens_out=15, reasoning_tokens=3,
                extra={"calls": 1, "handoff": "adk->crewai", "role": "critic"},
            ),
        ],
    )

    totals = runner.event_totals(RUN_ID)

    assert totals["tokens_in"] == 90
    assert totals["tokens_out"] == 15
    assert totals["reasoning_tokens"] == 3
    assert totals["model_calls"] == 1


def test_adk_usage_and_distinct_counts_are_preserved(monkeypatch):
    use_events(
        monkeypatch,
        [
            make_event(
                "model_call", "planner", framework="adk",
                tokens_in=500, tokens_out=100, reasoning_tokens=20,
            ),
            make_event(
                "model_call", "writer",
                tokens_in=800, tokens_out=200, reasoning_tokens=30,
            ),
            make_event(
                "a2a_call", "writer", framework="adk",
                tokens_in=800, tokens_out=200, reasoning_tokens=30,
            ),
            make_event(
                "a2a_call", "analyst", framework="adk",
                tokens_in=300, tokens_out=80, reasoning_tokens=8,
            ),
            make_event("tool_call", "researcher", framework="adk"),
            make_event("recovery", "analyst", framework="adk"),
            make_event("error", "critic", framework="adk", ok=False, error_type="A2AError"),
        ],
    )

    totals = runner.event_totals(RUN_ID)

    assert totals["tokens_in"] == 1600
    assert totals["tokens_out"] == 380
    assert totals["reasoning_tokens"] == 58
    assert totals["model_calls"] == 2
    assert totals["tool_calls"] == 1
    assert totals["recovery_events"] == 1
    assert totals["errors"] == 1


def test_repeated_role_events_resolve_independently_per_agent(monkeypatch):
    use_events(
        monkeypatch,
        [
            # Analyst ran twice (initial pass plus recovery) with no ack events.
            make_event(
                "model_call", "analyst",
                tokens_in=100, tokens_out=20, reasoning_tokens=4,
            ),
            make_event("recovery", "analyst", framework="adk"),
            make_event(
                "model_call", "analyst",
                tokens_in=50, tokens_out=10, reasoning_tokens=2,
            ),
            # Writer's two mirrored calls must contribute their model usage once.
            make_event(
                "model_call", "writer",
                tokens_in=200, tokens_out=50, reasoning_tokens=5,
            ),
            make_event(
                "a2a_call", "writer", framework="adk",
                tokens_in=200, tokens_out=50, reasoning_tokens=5,
            ),
            make_event(
                "model_call", "writer",
                tokens_in=200, tokens_out=50, reasoning_tokens=5,
            ),
            make_event(
                "a2a_call", "writer", framework="adk",
                tokens_in=200, tokens_out=50, reasoning_tokens=5,
            ),
            # Critic only produced acks, so both of its calls count.
            make_event(
                "a2a_call", "critic", framework="adk",
                tokens_in=90, tokens_out=10, reasoning_tokens=1,
            ),
            make_event(
                "a2a_call", "critic", framework="adk",
                tokens_in=30, tokens_out=5, reasoning_tokens=0,
            ),
        ],
    )

    totals = runner.event_totals(RUN_ID)

    assert totals["tokens_in"] == 670
    assert totals["tokens_out"] == 145
    assert totals["reasoning_tokens"] == 17
    assert totals["model_calls"] == 4
    assert totals["recovery_events"] == 1
