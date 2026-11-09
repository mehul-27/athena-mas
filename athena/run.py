"""Run the Athena pipeline: ``python -m athena.run "<question>" [--pattern NAME]``.

``--eval-id N`` pulls question N from the NASA evaluation set; ``--compare`` runs
the same question under every pattern and prints one table.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from dataclasses import asdict
import sys
import time

from google.adk.agents.run_config import RunConfig
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.runners import InMemoryRunner
from google.genai import types

from athena.agents import a2a_client
from athena.agents.claim_support import claim_problems, validate_claims
from athena.agents.confidence import finalize_confidence
from athena.agents.lifecycle import close_toolsets
from athena.agents.retry_audit import RetryAudit
from athena.agents.tool_report import build_tool_report
from athena.observability import Recorder, set_recorder
from athena.observability.adk_plugin import MonitoringPlugin
from athena.observability.hooks import install_hooks
from athena.agents.a2a_stage import DEFAULT_CARD_URL
from athena.agents.supervisor import build_supervisor
from athena.config.caps import cap_message, llm_call_cap
from athena.config.llm import configure_process
from athena.blackboard import Blackboard, Task
from athena.guardrails.input import refusal_message, screen_input
from athena.observability.guardrail_events import record_flags
from athena.patterns import VALID_PATTERNS, UnknownPatternError
from athena.run_report import ack_warning, parts_of, print_report, text_of, tool_lines
from athena.summary import (
    RunSummary, apply_authoritative_counts, count_evidence, finish, format_table,
    mark_degraded, note_ack, note_critique, note_recovered, set_outcome,
    set_terminal, summary_line,
)
from athena.agents.sufficiency import assess
from athena.agents.toolset_health import note_on_summary as note_toolset_failures
from athena.config.tool_call_guard import note_on_summary as note_tool_call_rejections
from athena.tools import registry, settings

# ADK derives the app name from the root agent's package ("athena/agents"); use
# the same name so it does not warn about a mismatch.
APP_NAME = "agents"
USER_ID = "student"


def question_from_args(args: argparse.Namespace) -> str:
    if args.eval_id is not None:
        path = settings.eval_dir() / "nasa_rag_40_questions.csv"
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row["id"] == str(args.eval_id):
                    return row["question"]
        raise SystemExit(f"no question with id {args.eval_id} in {path}")
    if not args.question:
        raise SystemExit('usage: python -m athena.run "<question>" [--pattern NAME] | --eval-id N')
    return args.question


def server_is_up(card_url: str = DEFAULT_CARD_URL) -> bool:
    import httpx

    try:
        return httpx.get(card_url, timeout=3.0).status_code == 200
    except Exception:  # noqa: BLE001 - any failure means "not up"
        return False


def default_runner(run_id: str, pattern: str, recorder) -> "InMemoryRunner":
    """The real ADK runner. Tests inject a stub factory instead."""
    return InMemoryRunner(
        agent=build_supervisor(run_id, pattern=pattern),
        app_name=APP_NAME,
        plugins=[MonitoringPlugin(recorder)],
    )


async def _execute(
    question: str,
    pattern: str,
    verbose: bool = True,
    runner_factory=None,
    run_id: str | None = None,
    tools: list[str] | tuple[str, ...] | None = None,
) -> RunSummary:
    verdict = screen_input(question, emit=False)
    # Only the screened remainder becomes run state or model input. The original
    # text stays out of this run's blackboard and guardrail audit details.
    safe_question = verdict.model_text if not verdict.blocking else ""
    blackboard = Blackboard.create(
        Task(
            question=safe_question,
            pattern=pattern,
            run_id=run_id or "",
            tools=list(tools) if tools is not None else None,
        )
    )
    record_flags(
        verdict.flags,
        screen="athena.run",
        pipeline_stage="input",
        run_id=blackboard.run_id,
    )
    recorder = Recorder.for_run(blackboard.run_id)
    if verdict.blocking:
        reason = "input rejected by guardrails"
        summary = RunSummary(pattern=pattern, run_id=blackboard.run_id)
        summary.failed = True
        summary.stage_errors.append(reason)
        summary.outcome = "failed"
        mark_degraded(summary, reason)
        set_terminal(summary, "failure", reason)
        _emit_summary(recorder, summary, pattern)
        if verbose:
            print(refusal_message(verdict), file=sys.stderr)
        return summary

    # The plugin in `athena/observability` infers a tool retry from event order,
    # which it cannot do correctly: a *different* call to the same tool that
    # succeeds later is not a retry, and neither is a guard-blocked call. The
    # audit sits in front of the recorder and forwards everything except an
    # inferred retry no recorded call supports.
    audited = RetryAudit(recorder)
    set_recorder(audited)
    install_hooks()

    build_runner = runner_factory or default_runner
    runner = build_runner(blackboard.run_id, pattern, audited)
    summary = RunSummary(pattern=pattern, run_id=blackboard.run_id)
    started = time.perf_counter()

    try:
        session = await runner.session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID
        )
        a2a_client.reset_stats()

        if verbose:
            print(
                f"\nrun_id: {blackboard.run_id}\nquestion: {safe_question}\n"
                f"pattern: {pattern}\nstages:"
            )

        # A hard ceiling on model calls: a model that keeps re-calling a refused
        # tool would otherwise loop until the provider quota runs out.
        cap = llm_call_cap(pattern)
        run_config = RunConfig(max_llm_calls=cap)

        try:
            async for event in runner.run_async(
                user_id=USER_ID,
                session_id=session.id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text=safe_question)]
                ),
                run_config=run_config,
            ):
                _note_event(summary, event, verbose)

            summary.wall_s = time.perf_counter() - started
            _finalize(summary, blackboard)
        except asyncio.CancelledError:
            # Cancellation is a terminal state, not a silent stop: record it and
            # let it propagate so the caller's own cleanup runs.
            summary.wall_s = time.perf_counter() - started
            summary.failed = True
            summary.stage_errors.append("run cancelled")
            mark_degraded(summary, "the run was cancelled")
            set_terminal(summary, "cancelled", "the run was cancelled before it finished")
            _count_artifacts(summary, blackboard)
            _emit_summary(recorder, summary, pattern)
            raise
        except TimeoutError as exc:
            summary.wall_s = time.perf_counter() - started
            summary.failed = True
            summary.stage_errors.append(f"timeout: {exc}" if str(exc) else "timeout")
            mark_degraded(summary, f"stage timeout: {exc or 'a stage exceeded its deadline'}")
            set_terminal(summary, "timeout", str(exc) or "a stage exceeded its deadline")
            recorder.emit(
                "error", "supervisor", "adk", ok=False,
                error_type="StageTimeout", error_message=str(exc)[:200],
                payload_summary=f"{pattern}: run timed out",
            )
            _finalize(summary, blackboard, keep_terminal=True)
        except LlmCallsLimitExceededError:
            summary.failed = True
            summary.wall_s = time.perf_counter() - started
            mark_degraded(summary, f"llm call cap {llm_call_cap(pattern)} reached")
            set_terminal(summary, "failure", "llm call cap reached")
            _count_artifacts(summary, blackboard)
            raise
        except Exception as exc:
            summary.failed = True
            summary.wall_s = time.perf_counter() - started
            summary.stage_errors.append(f"{type(exc).__name__}: {exc}")
            mark_degraded(summary, f"{type(exc).__name__}: {exc}")
            set_terminal(summary, "failure", f"{type(exc).__name__}: {exc}")
            recorder.emit(
                "error", "supervisor", "adk", ok=False,
                error_type=type(exc).__name__, error_message=str(exc)[:200],
                payload_summary=f"{pattern}: run failed",
            )
            # A run that died still recorded whatever evidence it did collect.
            # Counting it here is what keeps a failed run's real artifacts
            # visible instead of reporting the zero a stale counter left behind.
            _count_artifacts(summary, blackboard)
            # The exception still propagates (the caller decides what to do), but
            # it carries the summary so the CLI can write the run's single
            # terminal state instead of losing it with the traceback.
            _carry_summary(exc, summary)
            raise
        finally:
            # The runner's agent owns every MCP stdio toolset in this process.
            # Closing them here is what makes the process actually exit after the
            # summary is written.
            await close_toolsets(getattr(runner, "agent", None))
    finally:
        set_recorder(None)

    if verbose:
        print_report(blackboard, summary.wall_s)
    return summary


SUMMARY_ATTR = "athena_run_summary"


def _carry_summary(exc: BaseException, summary: RunSummary) -> None:
    """Attach a run's summary to the exception that ended it."""
    try:
        setattr(exc, SUMMARY_ATTR, summary)
    except (AttributeError, TypeError):  # pragma: no cover - exotic exception types
        pass


def carried_summary(exc: BaseException) -> RunSummary | None:
    """The summary a raised run carried, when it has one."""
    summary = getattr(exc, SUMMARY_ATTR, None)
    return summary if isinstance(summary, RunSummary) else None


def failed_summary(
    pattern: str, run_id: str = "", reason: str = "", state: str = "failure"
) -> RunSummary:
    """A summary for a run that ended before it could write one itself."""
    summary = RunSummary(pattern=pattern, run_id=run_id)
    summary.failed = True
    if reason:
        summary.stage_errors.append(reason)
    mark_degraded(summary, reason or f"the run ended in {state}")
    set_terminal(summary, state, reason or f"run ended in {state}")
    return summary


def _count_artifacts(summary: RunSummary, blackboard: Blackboard) -> RunSummary:
    """Count a run's real artifacts, for a run that ended before ``_finalize``.

    A cancelled, capped or crashed run still recorded whatever evidence and
    report it managed to write. Reading them from the blackboard here is what
    keeps those numbers describing the artifacts that exist instead of the zeros
    a stale counter left behind.
    """
    return apply_authoritative_counts(summary, blackboard)


def _note_event(summary: RunSummary, event, verbose: bool) -> None:
    """Fold one runner event into the summary (stages, tokens, tool calls)."""
    metadata = event.custom_metadata or {}
    if "stage" in metadata:
        summary.stage_ms[metadata["stage"]] = metadata["stage_ms"]
        if verbose:
            print(f"  {metadata['stage']:<13} {metadata['stage_ms'] / 1000:6.1f}s")
    if "revision_rounds" in metadata:
        summary.revision_rounds = metadata["revision_rounds"]
    if "recovery_rounds" in metadata:
        summary.recovery_rounds = metadata["recovery_rounds"]
    if metadata.get("recovery_reason"):
        summary.recovery_reason = str(metadata["recovery_reason"])
    if metadata.get("recovery_skipped_reason"):
        summary.recovery_skipped_reason = str(metadata["recovery_skipped_reason"])
    if "confidence_notes" in metadata:
        summary.confidence_notes = [str(item) for item in metadata["confidence_notes"]]
    if verbose:
        for line in tool_lines(event):
            print(f"    {line}")
        text = text_of(event)
        if "a2a_payload" in metadata:
            print(f"    [a2a request] {metadata['a2a_payload_bytes']}B {metadata['a2a_payload']}")
        if event.author in {"analyst", "writer", "critic"} and text.startswith("{"):
            note_ack(summary, text)
            print(f"    [a2a] {text[:160]}")
            if warning := ack_warning(text):
                print(warning)
    elif event.author in {"analyst", "writer", "critic"}:
        text = text_of(event)
        if text.startswith("{"):
            note_ack(summary, text)

    summary.tool_calls += sum(1 for part in parts_of(event) if part.function_call)
    usage = getattr(event, "usage_metadata", None)
    if usage is not None:
        summary.adk_tokens += int(getattr(usage, "prompt_token_count", 0) or 0)


def _note_budget_stop(summary: RunSummary, blackboard: Blackboard) -> None:
    """A worker that hit its own budget lost work: say so, with the reason.

    ``athena.agents.budget`` records one ``budget_stop`` error event when it cuts
    a worker's model loop short. That is a real truncation of the research, so
    the run is degraded and the reason names the worker.
    """
    try:
        from athena.observability.store import SQLiteEventStore

        events = SQLiteEventStore.from_env().events(blackboard.run_id)
    except Exception:  # noqa: BLE001 - a status read must not break a run
        return
    stopped = [
        str(getattr(event, "agent", "") or "worker")
        for event in events
        if event.kind == "error" and getattr(event, "error_type", "") == "budget_stop"
    ]
    if stopped:
        mark_degraded(
            summary,
            "worker budget stopped the loop early: " + ", ".join(dict.fromkeys(stopped)),
        )


def _finalize(
    summary: RunSummary,
    blackboard: Blackboard,
    keep_terminal: bool = False,
) -> RunSummary:
    """Post-run bookkeeping: tools, evidence coverage, claims, confidence."""
    traffic = a2a_client.stats()
    summary.a2a_bytes_out = traffic["out"]
    summary.a2a_bytes_in = traffic["in"]

    evidence = blackboard.read("evidence")
    # The blackboard is the authoritative store: count what it actually holds.
    # The counters are re-read at the end of this function, once sanitization and
    # the confidence rewrite have touched the report, so the numbers describe the
    # artifacts that were finally shipped.
    summary.evidence_count = count_evidence(blackboard)
    task = blackboard.read("task")
    # A toolset that never loaded is a lost capability, not a quiet zero. It is
    # recorded as an error event and mirrored onto the summary, so a run with no
    # research tools can never be reported as a clean success.
    summary.toolset_failures = note_toolset_failures(
        summary, blackboard.run_id, blackboard.store
    )
    # A provider that refused a tool call is a bounded degradation, never a
    # clean run: it is reported with its reason so it cannot be read as one.
    rejections = note_tool_call_rejections(summary, blackboard.run_id, blackboard.store)
    if rejections:
        mark_degraded(
            summary,
            "the provider rejected a tool call "
            f"({len(rejections)} recorded rejection(s)); tool use was limited",
        )
    report = build_tool_report(blackboard.run_id, blackboard.store)
    summary.tools_enabled = list(report.get("enabled") or [])
    summary.tools_planned = list(report.get("planned") or [])
    summary.tools_invoked = list(report.get("invoked") or [])
    summary.tools_succeeded = list(report.get("succeeded") or [])
    summary.tools_failed = list(report.get("failed") or [])
    summary.tools_usable_evidence = list(report.get("usable_evidence") or [])
    summary.tools_skipped = list(report.get("skipped") or [])
    if report.get("calls"):
        summary.tool_calls = len(report["calls"])
    for call in report.get("calls") or []:
        if call.get("ok"):
            continue
        if call.get("blocked_by") == "worker_assignment":
            # A worker asking for a tool it was never assigned is a guard
            # outcome, not a runtime failure of the research stage.
            continue
        detail = f"{call.get('tool', 'tool')}: {call.get('error') or 'failed'}"
        if detail not in summary.stage_errors:
            summary.stage_errors.append(detail)
        # One invocation that failed while its stage carried on is a recorded
        # event, not a degradation: it lost no work, and the pilot's approved run
        # with three real citations must not be labelled degraded because one
        # fetch answered HTTP 403. A run that genuinely lost evidence is caught
        # by the sufficiency check and by the outcome below.
        note_recovered(summary, detail)
        if call.get("blocked_by") == "budget":
            # The worker's own tool budget cut it short: that is a real stop.
            mark_degraded(
                summary,
                f"{call.get('tool', 'tool')}: worker tool budget stopped the loop",
            )
    _note_budget_stop(summary, blackboard)

    assessment = assess(
        task[0].question if task else "",
        evidence[0] if evidence else [],
        enabled_tools=task[0].tools if task else None,
        tool_report=report,
    )
    summary.evidence_sufficient = assessment.ok
    summary.unmet_requirements = list(assessment.issues)
    summary.evidence_limitations = list(assessment.limitations)
    summary.subject_specific = assessment.subject_specific
    summary.general_background = assessment.background
    summary.answer_as_asked = assessment.answer_as_asked
    summary.qualified_possible = assessment.qualified_possible
    summary.supported_aspects = list(assessment.supported_aspects)
    summary.missing_aspects = list(assessment.missing_aspects)
    summary.missing_subjects = list(assessment.missing_subjects)
    summary.missing_requirements = list(assessment.missing_requirements)
    summary.conflict_notes = [item.note for item in assessment.conflicts]
    summary.evidence_quality = assessment.evidence_quality
    summary.confidence_ceiling = assessment.confidence_ceiling

    # Claim-level validation of the answer that actually shipped.
    current = blackboard.read("final_report")
    if current:
        checks = validate_claims(
            [current[0].answer, *current[0].key_points],
            evidence[0] if evidence else [],
            task[0].question if task else "",
            current[0].citations,
        )
        summary.claim_checks = len(checks)
        summary.claim_issues = list(claim_problems(checks))

    # Confidence is recomputed after sanitization and after the critic reviewed
    # the report, then read back: the summary and the report always agree.
    decision = finalize_confidence(blackboard, blackboard.run_id, author="supervisor")
    if decision is not None:
        summary.confidence_notes = list(decision.notes)
    report_now = blackboard.read("final_report")
    summary.confidence = report_now[0].confidence if report_now else None
    # The counters are read once more, now that sanitization and the confidence
    # rewrite have stopped touching the report. `citation_count` and `tools_used`
    # therefore describe the report that actually shipped: the pilot's summary
    # said 0 citations while the shipped report carried 3, because it was read
    # before the report existed.
    apply_authoritative_counts(summary, blackboard)
    critique = blackboard.read("critique")
    note_critique(summary, critique[0] if critique else None)
    if not keep_terminal:
        # set_outcome classifies the run and stamps its terminal state, so it has
        # to run before finish(): the first terminal state wins, and a run that
        # will be classified "approved" must not be latched as "failure" by the
        # default outcome.
        set_outcome(summary, report_now is not None, assessment.ok)
    finish(summary)
    return summary


def _emit_summary(recorder, summary: RunSummary, pattern: str) -> None:
    recorder.emit(
        "stage_end",
        "supervisor",
        "adk",
        payload_summary=f"run_summary {pattern} terminal {summary.terminal_state}",
        extra=asdict(summary),
    )



async def run_one(
    question: str,
    pattern: str,
    run_id: str = "",
    tools: list[str] | tuple[str, ...] | None = None,
) -> RunSummary:
    if not screen_input(question, emit=False).blocking:
        configure_process()  # export the endpoint and arm the guard
    try:
        summary = await _execute(
            question, pattern, verbose=True, run_id=run_id or None, tools=tools
        )
    except LlmCallsLimitExceededError:
        return _cap_reached(pattern)
    print("\n" + summary_line(summary))
    return summary


def _cap_reached(pattern: str) -> RunSummary:
    """A capped run is a failed run: say so plainly, no traceback."""
    cap = llm_call_cap(pattern)
    print("\n" + cap_message(pattern, cap), file=sys.stderr)
    recorder = Recorder.for_run(f"cap-{pattern}")
    recorder.emit(
        "error", "supervisor", "adk", ok=False,
        error_type="LlmCallsLimitExceededError",
        error_message=f"cap {cap} reached for pattern {pattern}",
    )
    summary = RunSummary(pattern=pattern, run_id=f"cap-{pattern}")
    summary.failed = True
    summary.stage_errors.append(f"llm call cap {cap} reached")
    mark_degraded(summary, f"llm call cap {cap} reached")
    set_terminal(summary, "failure", f"llm call cap {cap} reached")
    return summary


async def run_compare(
    question: str,
    tools: list[str] | tuple[str, ...] | None = None,
) -> list[RunSummary]:
    summaries = []
    for pattern in VALID_PATTERNS:
        summary = await _execute(question, pattern, verbose=False, tools=tools)
        print(f"{pattern:<13} done in {summary.wall_s:.0f}s")
        print(summary.stage_line())
        summaries.append(summary)
    print("\n" + format_table(summaries))
    return summaries


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="athena.run")
    parser.add_argument("question", nargs="?", help="the research question")
    parser.add_argument("--eval-id", type=int, help="question id from the NASA eval CSV")
    parser.add_argument("--pattern", default="sequential", help=f"one of {list(VALID_PATTERNS)}")
    parser.add_argument("--compare", action="store_true", help="run every pattern on this question")
    # Used by the demo API: it needs the run id before the run starts.
    parser.add_argument("--run-id", default="", help="use this run id instead of a new one")
    parser.add_argument("--json-summary", default="", help="write the RunSummary as JSON here")
    parser.add_argument(
        "--tools",
        default=None,
        help=(
            "comma-separated research tool ids; omit for all tools, pass an "
            "empty string for no tools"
        ),
    )
    args = parser.parse_args(argv)

    if args.pattern not in VALID_PATTERNS:
        raise SystemExit(f"unknown pattern {args.pattern!r}; choose one of {list(VALID_PATTERNS)}")

    selected_tools: tuple[str, ...] | None = None
    if args.tools is not None:
        try:
            selected_tools = registry.normalise_selection(
                [item for item in str(args.tools).split(",") if item.strip()]
            )
        except registry.UnknownToolError as exc:
            raise SystemExit(str(exc)) from exc

    question = question_from_args(args)
    input_blocked = screen_input(question, emit=False).blocking
    if not input_blocked and not server_is_up():
        print(
            "The CrewAI A2A server is not answering on 127.0.0.1:9001.\n"
            "Start it with:  python scripts/dev_up.py",
            file=sys.stderr,
        )
        return 1

    summary: RunSummary | None = None
    try:
        if args.compare and input_blocked:
            # A blocked comparison has no pattern results to compare. Create one
            # audited terminal run and preserve the usual non-zero CLI status.
            summary = asyncio.run(
                run_one(
                    question,
                    args.pattern,
                    run_id=args.run_id,
                    tools=selected_tools,
                )
            )
        elif args.compare:
            asyncio.run(run_compare(question, selected_tools))
        else:
            summary = asyncio.run(
                run_one(
                    question,
                    args.pattern,
                    run_id=args.run_id,
                    tools=selected_tools,
                )
            )
    except UnknownPatternError as exc:
        raise SystemExit(str(exc)) from exc
    except (asyncio.CancelledError, KeyboardInterrupt):
        # An interrupted run is a cancelled run, not a silent stop: it still ends
        # in exactly one terminal state, and that state is written out.
        summary = _report_terminal(
            args,
            failed_summary(
                args.pattern, args.run_id, "the run was interrupted", "cancelled"
            ),
        )
    except Exception as exc:  # noqa: BLE001 - one run must not end without a summary
        reason = f"{type(exc).__name__}: {exc}"
        print(f"\nrun failed: {reason}", file=sys.stderr)
        summary = _report_terminal(
            args, carried_summary(exc) or failed_summary(args.pattern, args.run_id, reason)
        )

    if summary is None:
        return 0
    if args.json_summary:
        write_summary_json(summary, args.json_summary)
    return 2 if summary.failed else 0


def _report_terminal(args: argparse.Namespace, summary: RunSummary) -> RunSummary:
    """Print and persist the terminal state of a run that ended early."""
    print("\n" + summary_line(summary), file=sys.stderr)
    return summary


def write_summary_json(summary: RunSummary, path: str) -> None:
    """Write the RunSummary so the demo API can read it without the blackboard."""
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(summary), indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
