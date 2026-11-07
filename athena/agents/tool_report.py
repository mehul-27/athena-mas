"""Per-invocation tool accounting for one research run.

The event log remains the source of truth, but a tool-level "succeeded and
failed" flag is not enough: one run can contain a successful fetch, a throttled
search, a worker-assignment refusal and a budget stop. This module keeps every
attempt, classifies why it was blocked, and separates attempted from usable.

A tool call event written by an older build may still contain the synthetic
"blocked by tool selection" duplicate emitted before the monitoring plugin
recorded the real blocked call. Those pairs are collapsed here; new runs emit
one event per invocation.
"""

from __future__ import annotations

import re
from typing import Any

from athena.tools import registry

_FORECAST_HINT = re.compile(
    r"(?<![a-z])(?:forecast|predict|projection|projected|trend|extrapolat)(?![a-z])",
    re.IGNORECASE,
)
_CALC_HINT = re.compile(
    r"(?<![a-z])(?:calculate|compute|arithmetic|numbers?|equation|formula|"
    r"thrust|isp|specific impulse|velocity|mass flow|pressure|temperature|"
    r"ratio|energy|budget)(?![a-z])",
    re.IGNORECASE,
)
_FETCH_HINT = re.compile(
    r"(?<![a-z])(?:fetch|read (?:the )?(?:page|url)|https?://)(?![a-z])",
    re.IGNORECASE,
)
_SEARCH_HINT = re.compile(
    r"(?<![a-z])(?:search|web search|online|current|latest)(?![a-z])",
    re.IGNORECASE,
)
_RAG_HINT = re.compile(
    r"(?<![a-z])(?:rag|corpus|local document|supplied document|handbook)(?![a-z])",
    re.IGNORECASE,
)
_SUMMARY_URL = re.compile(r"'url':\s*'([^']+)'")

_BLOCK_TYPES = {
    "ToolDisabled": "user_selection",
    "ToolNotAssigned": "worker_assignment",
    "ToolBudgetBlocked": "budget",
    "ToolDuplicateBlocked": "duplicate",
}


def _events(run_id: str) -> list[Any]:
    try:
        from athena.observability.store import SQLiteEventStore

        return SQLiteEventStore.from_env().events(run_id)
    except Exception:  # noqa: BLE001 - a status view must not break a run
        return []


def _planned_tools(roles: list[str], plan_text: str) -> list[str]:
    """Tools the plan steps actually imply, not every tool their owner owns.

    A calculation role that is asked to "compute the numbers" does not imply a
    forecast. If a step gives no tool-specific wording, its role's tools remain
    a conservative fallback.
    """
    hints = {
        "rag_search": bool(_RAG_HINT.search(plan_text)),
        "web_search": bool(_SEARCH_HINT.search(plan_text)),
        "fetch_page": bool(_FETCH_HINT.search(plan_text)),
        "calculate": bool(_CALC_HINT.search(plan_text)),
        "forecast": bool(_FORECAST_HINT.search(plan_text)),
    }
    planned: list[str] = []
    for role in roles:
        owned = list(registry.ROLE_TOOL_IDS.get(role, ()))
        hinted = [tool for tool in owned if hints.get(tool)]
        if hinted:
            planned.extend(hinted)
        else:
            # Do not infer a forecast from a vague calculation request.
            planned.extend(tool for tool in owned if tool != "forecast")
    return [tool for tool in registry.ALL_TOOL_IDS if tool in planned]


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _call_block(call: dict[str, Any], enabled: set[str]) -> str:
    blocked = call.get("blocked_by")
    if blocked in {
        "user_selection", "worker_assignment", "policy", "budget", "duplicate"
    }:
        return blocked
    if call.get("error_type") in _BLOCK_TYPES:
        return _BLOCK_TYPES[call["error_type"]]
    if call.get("result_status") == "blocked":
        return "policy"
    if not call.get("ok"):
        role = call.get("agent") or ""
        tool = call.get("tool") or ""
        if role in registry.ROLE_TOOL_IDS and tool not in registry.ROLE_TOOL_IDS[role]:
            # Older events used the generic "not enabled" message even when the
            # tool was enabled for the run but not assigned to this worker.
            return "worker_assignment" if tool in enabled else "user_selection"
        if call.get("error_type") == "ToolDisabled":
            return "user_selection"
    return ""


def _has_usable_evidence(call: dict[str, Any]) -> bool:
    if call.get("telemetry_conflict"):
        return False
    if not call.get("ok") or call.get("blocked_by"):
        return False
    status = call.get("result_status")
    if call.get("tool") == "fetch_page":
        return status in {None, "", "ok"}
    if call.get("tool") in {"web_search", "rag_search"}:
        return bool(call.get("sources"))
    if call.get("tool") in {"calculate", "forecast"}:
        return True
    return True


def _call_outcome(call: dict[str, Any]) -> str:
    blocked = call.get("blocked_by")
    if blocked:
        return "blocked_" + blocked
    if call.get("telemetry_conflict"):
        return "telemetry_conflict"
    if not call.get("ok"):
        return "failed"
    return "succeeded_with_evidence" if call.get("usable_evidence") else "succeeded_without_evidence"


def build_tool_report(run_id: str, store=None) -> dict[str, Any]:
    """Build the run's tool report from the task, plan and event log."""
    from athena.blackboard import Blackboard

    blackboard = Blackboard.open(run_id, store)
    task = blackboard.read("task")
    task_value = task[0] if task else None
    selection = registry.normalise_selection(
        task_value.tools if task_value is not None else None
    )
    enabled = list(registry.enabled_ids(selection))

    plan = blackboard.read("plan")
    plan_value = plan[0] if plan else None
    roles: list[str] = []
    for step in plan_value.steps if plan_value else []:
        if step.assigned_to not in roles:
            roles.append(step.assigned_to)
    plan_text = "\n".join(
        f"{step.assigned_to}: {step.description}"
        for step in (plan_value.steps if plan_value else [])
    )
    planned = _planned_tools(roles, plan_text)
    planned_blocked = [tool for tool in planned if tool not in enabled]
    blocked_plan = list(plan_value.blocked) if plan_value else []
    assignments = {
        role: list(registry.ROLE_TOOL_IDS.get(role, ()))
        for role in roles
    }

    raw_calls: list[dict[str, Any]] = []
    skipped = list(blocked_plan)
    for event in _events(run_id):
        if event.kind == "tool_call":
            tool = registry.tool_ids_from_event_summary(event.payload_summary)
            if tool is None:
                continue
            extra = event.extra or {}
            summary = event.payload_summary or ""
            sources = list(extra.get("sources") or [])
            requested_url = str(extra.get("requested_url") or "")
            explicit_requested_url = bool(requested_url)
            if not requested_url:
                match = _SUMMARY_URL.search(summary)
                if match:
                    requested_url = match.group(1)
            source_url = ""
            if sources and isinstance(sources[0], dict):
                source_url = str(sources[0].get("url") or "")
            # New events carry requested_url/final_url explicitly and may
            # legitimately redirect across hosts. Only older events without
            # that metadata are checked for a summary/source mismatch.
            conflict = bool(
                tool == "fetch_page"
                and not explicit_requested_url
                and requested_url
                and source_url
                and _host(requested_url)
                and _host(source_url)
                and _host(requested_url) != _host(source_url)
            )
            call = {
                "event_id": getattr(event, "event_id", ""),
                "seq": getattr(event, "seq", 0),
                "tool": tool,
                "agent": event.agent,
                "ok": event.ok,
                "duration_ms": event.duration_ms,
                "summary": summary,
                "error": event.error_message or "",
                "error_type": event.error_type or "",
                "blocked_by": extra.get("blocked_by") or "",
                "result_status": extra.get("result_status") or "",
                "requested_url": requested_url,
                "source_url": source_url,
                "telemetry_conflict": conflict,
                "sources": sources,
            }
            call["blocked_by"] = call["blocked_by"] or _call_block(call, set(enabled))
            call["usable_evidence"] = _has_usable_evidence(call)
            call["outcome"] = _call_outcome(call)
            raw_calls.append(call)
        elif event.kind == "skip":
            text = (event.payload_summary or "").strip()
            if text and text not in skipped:
                skipped.append(text)

    # Collapse the old synthetic block event into the real attempted call. It
    # had no duration or arguments and was immediately followed by the real
    # blocked call from the monitoring plugin.
    calls: list[dict[str, Any]] = []
    duplicates_removed = 0
    for index, call in enumerate(raw_calls):
        synthetic = (
            call["duration_ms"] is None
            and call["blocked_by"] == "user_selection"
            and call["summary"].endswith("blocked by tool selection")
        )
        if synthetic and index + 1 < len(raw_calls):
            following = raw_calls[index + 1]
            if (
                following["tool"] == call["tool"]
                and following["agent"] == call["agent"]
                and following["seq"] == call["seq"] + 1
                and following["error"] == call["error"]
            ):
                duplicates_removed += 1
                continue
        calls.append(call)

    invoked = sorted({call["tool"] for call in calls})
    succeeded = sorted({
        call["tool"] for call in calls if call["outcome"].startswith("succeeded")
    })
    failed = sorted({
        call["tool"] for call in calls if call["outcome"] == "failed"
    })
    mixed = sorted(set(succeeded) & set(failed))
    usable = sorted({
        call["tool"] for call in calls if call["usable_evidence"]
    })
    no_usable = sorted({
        call["tool"] for call in calls
        if call["outcome"].startswith("succeeded") and not call["usable_evidence"]
    })
    telemetry_conflicts = [
        call for call in calls if call.get("telemetry_conflict")
    ]
    blocked_calls = {
        reason: [
            call for call in calls if call["blocked_by"] == reason
        ]
        for reason in (
            "user_selection", "worker_assignment", "policy", "budget", "duplicate"
        )
    }
    return {
        "run_id": run_id,
        "selection": list(selection) if selection is not None else None,
        "all_tools": selection is None,
        "enabled": enabled,
        "assigned_roles": roles,
        "assignments": assignments,
        "planned": planned,
        "planned_blocked": planned_blocked,
        "invoked": invoked,
        "succeeded": succeeded,
        "failed": failed,
        "mixed": mixed,
        "usable_evidence": usable,
        "no_usable_evidence": no_usable,
        "blocked": {
            reason: [call["tool"] for call in group]
            for reason, group in blocked_calls.items()
        },
        "blocked_counts": {
            reason: len(group) for reason, group in blocked_calls.items()
        },
        "skipped": skipped,
        "enabled_not_invoked": [tool for tool in enabled if tool not in invoked],
        "planned_not_invoked": [tool for tool in planned if tool not in invoked],
        "call_count": len(calls),
        "telemetry_duplicates_removed": duplicates_removed,
        "telemetry_conflicts": telemetry_conflicts,
        "calls": calls,
    }


def _failure_lines(report: dict[str, Any]) -> list[str]:
    lines = []
    for call in report.get("calls") or []:
        if call.get("blocked_by"):
            continue
        if not call.get("ok"):
            lines.append(
                f"{call['tool']} ({call['agent']}): "
                f"{call.get('error') or call.get('result_status') or 'failed'}"
            )
    return list(dict.fromkeys(lines))


def format_tool_status(
    report: dict[str, Any],
    assessment_issues=None,
    limitations=None,
) -> str:
    """Compact prompt block distinguishing enabled, assigned, attempted and usable."""
    lines = [
        "Tools enabled by the user: " + (", ".join(report.get("enabled") or []) or "(none)"),
        "Worker assignment: "
        + (
            "; ".join(
                f"{role}: {', '.join(tools) or '(none)'}"
                for role, tools in (report.get("assignments") or {}).items()
            )
            or "(no worker assigned)"
        ),
        "Tools the plan actually implies: "
        + (", ".join(report.get("planned") or []) or "(none)"),
        "Tools actually attempted: " + (", ".join(report.get("invoked") or []) or "(none)"),
    ]
    if report.get("planned_blocked"):
        lines.append(
            "Planned tools disabled by the user: "
            + ", ".join(report["planned_blocked"])
        )
    if report.get("mixed"):
        lines.append(
            "Tools with both successful and failed invocations: "
            + ", ".join(report["mixed"])
        )
    if report.get("usable_evidence"):
        lines.append(
            "Tools that returned usable evidence: "
            + ", ".join(report["usable_evidence"])
        )
    if report.get("no_usable_evidence"):
        lines.append(
            "Tools that returned no usable evidence: "
            + ", ".join(report["no_usable_evidence"])
        )
    conflicts = report.get("telemetry_conflicts") or []
    if conflicts:
        lines.append(
            "Invocation telemetry conflicts (recorded URL differs from result source): "
            + "; ".join(
                f"{call.get('tool')} by {call.get('agent')} at seq {call.get('seq')}"
                for call in conflicts
            )
        )
    blocked_labels = {
        "user_selection": "Blocked by user selection",
        "worker_assignment": "Blocked by worker assignment (not a user restriction)",
        "policy": "Blocked by policy",
        "budget": "Blocked by worker budget",
        "duplicate": "Blocked as duplicate",
    }
    for reason, label in blocked_labels.items():
        tools = sorted(set((report.get("blocked") or {}).get(reason) or []))
        if tools:
            lines.append(label + ": " + ", ".join(tools))
    failures = _failure_lines(report)
    if failures:
        lines.append("Failed invocations: " + "; ".join(failures))
    if not report.get("invoked"):
        lines.append(
            "No research tool was actually invoked. An enabled tool is not evidence "
            "that it ran, and a plan is not proof of execution."
        )
    skipped = report.get("skipped") or []
    if skipped:
        lines.append("Skipped or blocked plan steps: " + "; ".join(str(item) for item in skipped))
    for issue in assessment_issues or []:
        lines.append(f"Blocking evidence gap: {issue}")
    for item in limitations or []:
        lines.append(f"Non-blocking evidence limitation: {item}")
    return "\n".join(lines)
