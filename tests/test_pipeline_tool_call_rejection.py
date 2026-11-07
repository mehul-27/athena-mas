"""Regression tests for a provider-side tool-call rejection (``tool_use_failed``).

The pilot run died here::

    BadRequestError: GroqException - {"error":{"message":"tool call validation
    failed: attempted to call tool 'webollar' which was not in request.tools",
    "code":"tool_use_failed"}}

Three good evidence items were already on the blackboard. The run ended
``terminal_state=failure`` with no critic and no report because one corrupted
tool name was treated as a fatal pipeline error.

What is pinned here, against the real ``RateLimitedLiteLlm`` wrapper:

(a) an unknown-tool-name ``BadRequestError`` produces **exactly one** retry
    event, and the retried call carries a corrective instruction naming the
    permitted tools and their argument schemas;
(b) a second rejection **degrades** the stage instead of raising out of it, and
    the evidence gathered before the failure is preserved;
(c) no retry event is written when no retry happens.

Everything is offline: the provider call is replaced by a scripted stub, and no
tool, model or server is contacted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import litellm
import pytest
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from athena.blackboard import Blackboard, EvidenceItem, SQLiteStore, Task
from athena.config import tool_call_guard
from athena.config.limited_models import RateLimitedLiteLlm
from athena.observability.recorder import Recorder, set_recorder
from athena.observability.store import SQLiteEventStore
from athena.summary import RunSummary

# The exact message shape Groq returned. Kept verbatim: the recognition must
# match what a real provider sends, not a paraphrase of it.
UNKNOWN_TOOL_ERROR = (
    'GroqException - {"error":{"message":"tool call validation failed: attempted '
    'to call tool \'webollar\' which was not in request.tools","type":'
    '"invalid_request_error","code":"tool_use_failed","failed_generation":'
    '"<tool_call>\\n<function=webollar>\\n</function>\\n</tool_call>"}}'
)

QUESTION = "Which element has the chemical symbol W, and what is its atomic number?"
TUNGSTEN_URL = "https://en.wikipedia.org/wiki/Tungsten"
RUN_ID = "run-tool-reject"


def _rejection(message: str = UNKNOWN_TOOL_ERROR) -> litellm.BadRequestError:
    return litellm.BadRequestError(message, "groq/qwen/qwen3.8-27b", "groq")


@pytest.fixture()
def events(tmp_path) -> SQLiteEventStore:
    store = SQLiteEventStore(tmp_path / "events.db")
    set_recorder(Recorder(RUN_ID, store))
    yield store
    set_recorder(None)


def _request() -> LlmRequest:
    """A request offering exactly the tools the web researcher really offers."""
    schema = types.Schema(
        type="OBJECT",
        properties={
            "query": types.Schema(type="STRING"),
            "max_results": types.Schema(type="INTEGER"),
        },
        required=["query"],
    )
    declaration = types.FunctionDeclaration(
        name="web_search", description="Search the web.", parameters=schema
    )
    request = LlmRequest()
    request.config = types.GenerateContentConfig(
        tools=[types.Tool(function_declarations=[declaration])]
    )
    request.contents.append(
        types.Content(role="user", parts=[types.Part(text=QUESTION)])
    )
    return request


def _response(text: str = "DONE"):
    return SimpleNamespace(
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        finish_reason="STOP",
        usage_metadata=None,
    )


def _scripted(script: list, seen: list):
    """Patch target for ``LiteLlm.generate_content_async``.

    Each entry is either an exception to raise or a list of responses to yield.
    ``seen`` collects the request each attempt actually sent, so the corrective
    instruction can be read back from the request that was really made.
    """

    def fake(self, llm_request, stream=False):
        seen.append(llm_request)
        step = script[min(len(seen) - 1, len(script) - 1)]
        if isinstance(step, BaseException):
            async def boom():
                raise step
                yield  # pragma: no cover
            return boom()

        async def gen():
            for response in step:
                yield response

        return gen()

    return fake


def _drive(monkeypatch, script: list) -> tuple[list, list]:
    """Run the real wrapper over a scripted provider. Returns (responses, seen)."""
    seen: list = []
    monkeypatch.setattr(
        LiteLlm, "generate_content_async", _scripted(script, seen)
    )
    model = RateLimitedLiteLlm(model="groq/test", api_key="x", max_tokens=100)

    async def run():
        return [item async for item in model.generate_content_async(_request())]

    return asyncio.run(run()), seen


def _retries(store: SQLiteEventStore) -> list:
    return [event for event in store.events(RUN_ID) if event.kind == "retry"]


def _errors(store: SQLiteEventStore, error_type: str) -> list:
    return [
        event for event in store.events(RUN_ID)
        if event.kind == "error" and event.error_type == error_type
    ]


# --- recognition ------------------------------------------------------------


def test_the_real_groq_message_is_recognised_and_nothing_else_is() -> None:
    assert tool_call_guard.is_tool_call_validation_error(UNKNOWN_TOOL_ERROR)
    assert tool_call_guard.is_tool_call_validation_error(_rejection())

    # Not this class: these must keep propagating to the caller untouched.
    assert not tool_call_guard.is_tool_call_validation_error(
        "context_length_exceeded: maximum context length is 8192 tokens"
    )
    assert not tool_call_guard.is_tool_call_validation_error(
        "Invalid API key provided"
    )
    assert not tool_call_guard.is_tool_call_validation_error("rate limit reached")


def test_the_rejected_name_is_read_from_the_message_not_invented() -> None:
    # The corrupted name is recorded, never used as a tool.
    assert tool_call_guard.offending_tool(UNKNOWN_TOOL_ERROR) == "webollar"
    # Prose from the same message must not be mistaken for a tool name.
    assert tool_call_guard.offending_tool("tool call validation failed") == ""
    assert tool_call_guard.offending_tool("") == ""


# --- (a) one retry, with a corrective prompt --------------------------------


def test_unknown_tool_name_is_retried_once_with_the_permitted_tools(
    monkeypatch, events
) -> None:
    responses, seen = _drive(
        monkeypatch, [_rejection(), [_response("DONE")]]
    )

    # Exactly one retry: two model calls, and the correction reached the second.
    assert len(seen) == 2
    assert [part.text for r in responses for part in (r.content.parts or [])] == ["DONE"]

    correction = seen[1].contents[-1].parts[-1].text
    assert "TOOL CALL REJECTED BY THE PROVIDER" in correction
    assert "`webollar`" in correction
    # The exact permitted tool and its argument schema are listed.
    assert "`web_search`" in correction
    assert "query: string (required)" in correction
    assert "max_results: integer (optional)" in correction
    # And it never tells the model to invent anything.
    assert "Do not invent a tool name" in correction


def test_exactly_one_retry_event_is_written_for_that_retry(monkeypatch, events) -> None:
    _drive(monkeypatch, [_rejection(), [_response("DONE")]])

    retries = _retries(events)
    assert len(retries) == 1
    assert retries[0].extra["retry_type"] == "tool_call"
    assert retries[0].extra["attempt"] == 2
    assert retries[0].extra["error_type"] == "tool_use_failed"
    assert retries[0].extra["rejected_tool"] == "webollar"
    # It was reported, not inferred.
    assert "inferred" not in retries[0].extra

    # The rejection itself is recorded too, flagged as retried.
    rejections = _errors(events, "ToolCallRejected")
    assert len(rejections) == 1
    assert rejections[0].extra["will_retry"] is True
    assert _errors(events, "ToolCallDegraded") == []


# --- (b) the second failure degrades instead of raising ---------------------


def test_a_second_rejection_degrades_the_stage_without_raising(
    monkeypatch, events
) -> None:
    responses, seen = _drive(
        monkeypatch, [_rejection(), _rejection()]  # fail, fail, never a third call
    )

    # Never infinite: exactly the one allowed retry.
    assert len(seen) == 2
    # The stage received a usable response rather than an exception, so the
    # writer can still run over the evidence that was already gathered.
    assert len(responses) == 1
    text = "".join(part.text for part in responses[0].content.parts)
    assert text == tool_call_guard.DEGRADED_TEXT
    assert responses[0].custom_metadata["athena_degraded"] == "tool_call_rejected"

    # One retry happened, so exactly one retry event — and the degradation is
    # recorded separately so it cannot be read as a clean success.
    assert len(_retries(events)) == 1
    assert len(_errors(events, "ToolCallRejected")) == 2
    assert len(_errors(events, "ToolCallDegraded")) == 1


def test_the_evidence_gathered_before_the_failure_is_preserved(
    monkeypatch, tmp_path
) -> None:
    """Three recorded web sources survive a stage that ends degraded."""
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(
        Task(question=QUESTION, pattern="hierarchical"), store=store
    )
    # The same event store the summary reads back through EVENTS_DB, so the
    # recorded degradation is the one the run actually reports.
    set_recorder(Recorder(blackboard.run_id, SQLiteEventStore.from_env()))
    try:
        for index, url in enumerate(
            (
                "https://periodic-table.rsc.org/element/74/tungsten",
                TUNGSTEN_URL,
                "https://periodic-table.io/element-74",
            ),
            start=1,
        ):
            blackboard.append(
                "evidence",
                EvidenceItem(
                    id=f"web-{index}",
                    source_type="web",
                    source_ref=url,
                    content="Tungsten (W) has atomic number 74.",
                    retrieved_by="web_researcher",
                ),
                author="web_researcher",
            )

        responses, _seen = _drive(monkeypatch, [_rejection(), _rejection()])
        assert responses  # the stage degraded, it did not raise

        # The authoritative store still holds every item gathered before it.
        recorded = blackboard.read("evidence")
        assert recorded is not None
        assert len(recorded[0]) == 3

        # And the run's summary says so, instead of reporting zero.
        summary = RunSummary(pattern="hierarchical", run_id=blackboard.run_id)
        tool_call_guard.note_on_summary(
            summary, blackboard.run_id, blackboard.store
        )
        assert summary.degraded is True
        assert any("tool call rejected" in error for error in summary.stage_errors)
    finally:
        set_recorder(None)


def test_a_degraded_run_is_distinguishable_from_a_clean_one(
    monkeypatch, events
) -> None:
    clean = RunSummary(pattern="hierarchical", run_id="no-rejection-here")
    assert tool_call_guard.note_on_summary(clean, "no-rejection-here") == []
    assert clean.degraded is False

    _drive(monkeypatch, [_rejection(), _rejection()])
    degraded = RunSummary(pattern="hierarchical", run_id=RUN_ID)
    rows = tool_call_guard.note_on_summary(degraded, RUN_ID)

    assert rows and degraded.degraded is True
    assert degraded.stage_errors, "the reason must be readable, not just a flag"


# --- (c) no retry event when no retry happens -------------------------------


def test_a_successful_call_writes_no_retry_event(monkeypatch, events) -> None:
    responses, seen = _drive(monkeypatch, [[_response("DONE")]])

    assert len(seen) == 1
    assert responses
    assert _retries(events) == []
    assert _errors(events, "ToolCallRejected") == []


def test_a_non_tool_error_still_propagates_and_is_not_retried(
    monkeypatch, events
) -> None:
    """The guard is narrow: an unrelated provider failure is not swallowed."""
    with pytest.raises(litellm.BadRequestError):
        _drive(
            monkeypatch,
            [_rejection("context_length_exceeded: maximum context length is 8192")],
        )

    assert _retries(events) == []
    assert _errors(events, "ToolCallRejected") == []


def test_the_corrective_prompt_never_leaks_into_the_next_request(
    monkeypatch, events
) -> None:
    """The original request is copied, so the correction is per-call only."""
    responses, seen = _drive(monkeypatch, [_rejection(), [_response("DONE")]])
    assert responses
    # The request ADK owns is unchanged: one user turn, as it was built.
    assert len(seen[0].contents) == 1
    # Only the retried copy carries the correction.
    assert len(seen[1].contents) == 2


def test_the_retry_count_is_bounded_regardless_of_the_script(
    monkeypatch, events
) -> None:
    """A provider that would fail forever still costs two calls, not a loop."""
    _drive(monkeypatch, [_rejection()])  # the same step repeats forever
    assert len(_retries(events)) == 1
    # Two rejections recorded (one per attempt), plus the one degradation mark:
    # bounded, and every one of them describes something that happened.
    assert len(_errors(events, "ToolCallRejected")) == 2
    assert len(_errors(events, "ToolCallDegraded")) == 1
