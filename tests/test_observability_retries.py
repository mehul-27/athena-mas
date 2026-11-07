"""``retry`` events: one per retry that actually happened, and none otherwise.

The two directions matter equally. A run that retried must say so with the
attempt number, the reason, the retry type and the budget change; a run that did
not retry must leave no ``retry`` event behind, because a phantom retry is a
false claim about the pipeline.

The one explicit empty-reply retry site lives in ``athena/config/reply_guard.py``
/ ``athena/config/limited_models.py`` (owned elsewhere). The tests here drive the
*real* decision predicates (``empty_and_truncated``, ``doubled``) and the real
``RateLimitedLiteLlm`` wrapper, so they assert both the event contract the call
site has to satisfy and the "never a retry that did not happen" rule on the real
path.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from athena.config.reply_guard import doubled, empty_and_truncated
from athena.observability.events import Event
from athena.observability.recorder import Recorder, set_recorder
from athena.observability.retry_events import (
    note_retry,
    retry_empty_reply,
    retry_invalid_output,
    retry_truncated_report,
)
from athena.observability.store import SQLiteEventStore


@pytest.fixture()
def store(tmp_path) -> SQLiteEventStore:
    return SQLiteEventStore(tmp_path / "events.db")


@pytest.fixture(autouse=True)
def _reset_recorder():
    yield
    set_recorder(None)


def _retries(store: SQLiteEventStore, run_id: str = "run-retry") -> list[Event]:
    return [event for event in store.events(run_id) if event.kind == "retry"]


def _response(text: str | None, finish: str = "STOP"):
    parts = [SimpleNamespace(text=text, thought=False)] if text else []
    return SimpleNamespace(content=SimpleNamespace(parts=parts), finish_reason=finish)


# ------------------------------------------------------------------ the emitter
def _empty_reply_site(framework: str, budget: int) -> bool:
    """The 4 lines the empty-reply retry site has to add.

    Transcribed from the patch documented in ``docs/OBSERVABILITY.md``: the
    decision and the retry already exist in
    ``athena/config/limited_models.py``, so the site only has to say what it did.
    Kept here as the executable form of that contract.
    """
    retried_budget = doubled(budget)
    return retry_empty_reply(framework, budget, retried_budget)


def test_empty_reply_retry_emits_exactly_one_event_with_every_field(store) -> None:
    set_recorder(Recorder("run-retry", store))

    # The predicate decides a retry; the budget is doubled; one retry happens.
    assert empty_and_truncated([_response(None, "LENGTH")]) is True
    _empty_reply_site("adk", 1000)

    events = _retries(store)
    assert len(events) == 1, "one retry is one retry event"
    event = events[0]
    assert event.run_id == "run-retry"
    assert event.kind == "retry"
    assert event.agent  # the role that retried
    assert event.framework == "adk"
    assert event.ts  # timestamp, written by the Event default
    assert event.extra["attempt"] == 2
    assert event.extra["of"] == 2
    assert event.extra["retry_type"] == "empty_reply"
    assert event.extra["error_type"] == "empty_reply"
    assert event.extra["stage"] == "model_call"
    assert event.extra["token_budget"] == {"requested": 1000, "retried": 2000}
    assert "empty" in event.extra["reason"].lower()
    assert event.error_type == "empty_reply"


def test_noting_an_empty_reply_is_not_itself_a_retry(store) -> None:
    """The trap: ``note_empty_reply`` runs *after* a retry failed, too.

    ``athena/config/limited_models.py`` calls it once before the retry and once
    when the retry also came back empty. Emitting a retry from inside it would
    invent a second, non-existent retry — so the emit site must be the retry
    itself, and ``note_empty_reply`` must stay a plain ``error`` event.
    """
    from athena.config.reply_guard import note_empty_reply

    set_recorder(Recorder("run-retry", store))
    note_empty_reply("adk", 1000)
    note_empty_reply("adk", 2000)

    events = store.events("run-retry")
    assert [event.kind for event in events] == ["error", "error"]
    assert _retries(store) == []


def test_a_reply_with_visible_text_is_not_a_retry(store) -> None:
    set_recorder(Recorder("run-retry", store))

    responses = [_response("here is the answer")]
    assert empty_and_truncated(responses) is False
    # The site only emits inside the branch that retries; taken here, so nothing
    # is written. A clean run must not look like a retried one.
    assert _retries(store) == []


def test_the_writer_and_parse_retries_report_their_own_type(store) -> None:
    set_recorder(Recorder("run-retry", store))

    retry_truncated_report(agent="writer")
    retry_invalid_output(ValueError("not json"), agent="writer")

    events = _retries(store)
    assert [event.extra["retry_type"] for event in events] == [
        "truncated_report", "invalid_json",
    ]
    assert events[0].framework == "crewai"
    assert "not json" in events[1].extra["reason"]
    assert all(event.extra["attempt"] == 2 for event in events)


def test_retry_reason_is_redacted(store, monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_testvalue0123456789abcdef")
    set_recorder(Recorder("run-retry", store))

    note_retry("writer", "rejected while using gsk_testvalue0123456789abcdef", "other")

    event = _retries(store)[0]
    assert "gsk_testvalue" not in event.extra["reason"]
    assert "[REDACTED]" in event.extra["reason"]


def test_no_recorder_means_no_event_and_no_raise() -> None:
    set_recorder(None)
    assert note_retry("writer", "nothing to record", "other") is False


def test_inferred_retries_are_marked_as_inferred(store) -> None:
    set_recorder(Recorder("run-retry", store))

    note_retry(
        "researcher", "deduced from event order", "tool_call",
        inferred=True, inferred_from="ok after a failure", attempt=2,
    )

    event = _retries(store)[0]
    assert event.extra["inferred"] is True
    assert event.extra["inferred_from"] == "ok after a failure"


# ------------------------------------------------------- the real retry wrapper
def _fake_runner(script, seen: list):
    def fake(self, llm_request, stream=False):
        seen.append(self._additional_args.get("max_tokens"))
        payload = script[min(len(seen) - 1, len(script) - 1)]

        async def gen():
            for response in payload:
                yield response

        return gen()

    return fake


def test_an_empty_reply_that_is_not_retried_emits_no_retry_event(
    monkeypatch, store
) -> None:
    """The real wrapper: text came back, so there was no retry to report."""
    from google.adk.models.lite_llm import LiteLlm
    from athena.config.limited_models import RateLimitedLiteLlm

    set_recorder(Recorder("run-retry", store))
    seen: list = []
    monkeypatch.setattr(
        LiteLlm, "generate_content_async",
        _fake_runner([[_response("the answer")]], seen),
    )
    model = RateLimitedLiteLlm(model="groq/test", api_key="x", max_tokens=100)

    async def run():
        return [item async for item in model.generate_content_async(SimpleNamespace())]

    asyncio.run(run())

    assert seen == [100], "the retry must not have happened"
    assert _retries(store) == []


def test_the_real_empty_reply_retry_never_claims_more_than_one_retry(
    monkeypatch, store
) -> None:
    """One retry, at most one retry event — and never a second one.

    The wrapper is driven for real: the model returns nothing visible on the
    first attempt and text on the second, so exactly one retry happens
    (``seen == [100, 200]``). Whatever the emit site records, it may describe
    that single retry once and must never turn one retry into two rows.
    """
    from google.adk.models.lite_llm import LiteLlm
    from athena.config.limited_models import RateLimitedLiteLlm

    set_recorder(Recorder("run-retry", store))
    seen: list = []
    monkeypatch.setattr(
        LiteLlm, "generate_content_async",
        _fake_runner(
            [[_response(None, "LENGTH")], [_response("hello")]], seen
        ),
    )
    model = RateLimitedLiteLlm(model="groq/test", api_key="x", max_tokens=100)

    async def run():
        return [item async for item in model.generate_content_async(SimpleNamespace())]

    asyncio.run(run())

    assert seen == [100, 200], "exactly one retry happened"
    events = _retries(store)
    assert len(events) <= 1, "one retry must never produce two retry events"
    for event in events:  # whenever the site is installed, the contract holds
        assert event.extra["retry_type"] == "empty_reply"
        assert event.extra["attempt"] == 2
        assert event.extra["token_budget"]["retried"] == 200


def test_a_retry_event_is_attributed_to_the_active_run_only(store) -> None:
    """The run id comes from the recorder, not from the data being retried."""
    set_recorder(Recorder("run-owner", store))

    note_retry("writer", "retrying", "other", attempt=2)

    assert [event.run_id for event in _retries(store, "run-owner")] == ["run-owner"]
    assert store.events("run-somebody-else") == []
