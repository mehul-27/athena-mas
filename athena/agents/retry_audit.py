"""Only a retry that really happened is recorded, and the reason says why.

``MonitoringPlugin`` has no retry callback, so it *infers* a tool retry from
event order: a successful ``tool_call`` for a tool that failed earlier in the
same stage. That inference is wrong in the three cases the pilot produced:

* ``fetch_page https://en.wikipedia.org/wiki/Tungsten`` failed with HTTP 403 and a
  **later, different** call, ``fetch_page https://www.periodic-table.org/...``,
  succeeded. The run reported ``retry tool_call (attempt 2)`` although no call
  was ever repeated;
* a call blocked as a duplicate (or by a budget) is a deliberate guard outcome,
  not a failed call that gets tried again, yet it counted as one;
* the inference is keyed by ``(agent, tool)``, so two different calls to the same
  tool always collide.

The plugin lives in ``athena/observability`` (owned elsewhere), so this module
enforces the contract at the point where events are written: the runner installs
:class:`RetryAudit` in front of the run's recorder. Every event is forwarded
unchanged **except** an inferred tool retry that no recorded call supports.

A retry is let through only when the same agent called the same tool with the
same arguments again *after that exact call failed* — which is what "the same
work, tried again" means. A reported (non-inferred) retry is a fact the retrying
code emitted itself, and is never second-guessed here.
"""

from __future__ import annotations

from typing import Any

# Error types that are guard outcomes rather than a failed attempt of the work.
# Nothing was executed, so there is nothing to retry.
NOT_RETRYABLE = frozenset(
    {
        "ToolDuplicateBlocked",
        "ToolBudgetBlocked",
        "ToolNotAssigned",
        "ToolDisabled",
        "ToolArgsInvalid",
        "ToolCallRejected",
        "ToolCallDegraded",
    }
)


def _signature(fields: dict[str, Any]) -> str:
    """What the call actually asked for: the recorded ``tool: <args>`` summary."""
    text = " ".join(str(fields.get("payload_summary") or "").split())
    return text


class RetryAudit:
    """A ``Recorder`` that refuses to record an inferred retry that never happened.

    Delegates everything (``run_id``, ``store``, ``span``, ``failures`` …) to the
    real recorder, so it is a drop-in replacement wherever a ``Recorder`` is
    expected.
    """

    def __init__(self, recorder: Any) -> None:
        self._recorder = recorder
        # (agent, tool) -> arguments of every failed, retryable call not yet
        # matched by a repeat of the same call.
        self._failed: dict[tuple[str, str], list[str]] = {}
        # (agent, tool) -> arguments of the most recent successful call.
        self._last_ok: dict[tuple[str, str], str] = {}
        # How many phantom retries were refused. Exposed for tests and debugging;
        # refusing to write an event is the whole behaviour, so nothing is lost.
        self.dropped = 0

    def __getattr__(self, name: str) -> Any:
        # `_recorder` itself must come from the instance dict: looking it up
        # through this method would recurse before `__init__` finished.
        if name == "_recorder":
            raise AttributeError(name)
        return getattr(self._recorder, name)

    # --- the recorder API ------------------------------------------------
    def emit(
        self,
        kind: str,
        agent: str | None = None,
        framework: str = "adk",
        **fields: Any,
    ) -> None:
        if kind == "tool_call":
            self._observe_tool_call(agent, fields)
        elif kind == "retry":
            corrected = self._correct(agent, fields)
            if corrected is None:
                return
            fields = corrected
        self._recorder.emit(kind, agent, framework, **fields)

    # --- call bookkeeping ------------------------------------------------
    def _observe_tool_call(self, agent: str | None, fields: dict[str, Any]) -> None:
        extra = fields.get("extra") or {}
        tool = str(extra.get("tool") or "")
        if not tool:
            return
        key = (str(agent or ""), tool)
        signature = _signature(fields)
        if fields.get("ok", True):
            self._last_ok[key] = signature
            return
        # A guard outcome is not a failed attempt: it was never executed, so it
        # can never be retried.
        if extra.get("blocked_by"):
            return
        if str(fields.get("error_type") or "") in NOT_RETRYABLE:
            return
        self._failed.setdefault(key, []).append(signature)

    def _correct(
        self, agent: str | None, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The retry event to write, or ``None`` when no retry happened.

        An inferred tool retry is kept only when the same call (same agent, same
        tool, same arguments) failed before and is now succeeding, and it is
        re-stated with the attempt number and reason this call actually has: the
        plugin counts failures per tool, so its attempt number can be wrong when
        two *different* calls failed.
        """
        extra = fields.get("extra") or {}
        if not extra.get("inferred") or str(extra.get("retry_type") or "") != "tool_call":
            # Reported by the code that retried (or a model-call inference): a
            # fact this module has nothing to correct.
            return fields
        key = (str(agent or ""), str(extra.get("tool") or ""))
        failed = self._failed.get(key) or []
        repeated = self._last_ok.get(key)
        if repeated is None or repeated not in failed:
            self.dropped += 1
            return None
        previous = failed.count(repeated)
        for _ in range(previous):
            failed.remove(repeated)
        attempt = previous + 1
        corrected_extra = dict(extra)
        corrected_extra["attempt"] = attempt
        corrected_extra["reason"] = (
            f"tool {extra.get('tool') or 'call'} was called again with the same "
            f"arguments after failing {previous} time(s)"
        )
        corrected_extra["attempts_of_this_call_failed"] = previous
        return {
            **fields,
            "extra": corrected_extra,
            "payload_summary": f"retry tool_call (attempt {attempt})",
        }


__all__ = ["NOT_RETRYABLE", "RetryAudit"]
