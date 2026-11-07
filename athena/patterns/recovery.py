"""Bounded, aspect-targeted evidence recovery for the flat pipelines.

Hierarchical already owns a review loop. These patterns keep their existing
stage order; this small stage runs after their first critic and only acts when
the deterministic assessment still has an unanswered subject, requested part,
or an unestablished requirement.

Recovery is driven by *what is missing*, not by how much evidence there is:

* the specific gaps are named (missing subject, missing requested aspect, an
  unestablished requirement, an account of a requested conflict that no source
  supports);
* the queries are built from those gaps, so they cannot repeat a generic search
  that already failed;
* queries and URLs already attempted are skipped, including the ones that
  failed;
* the round count and the number of new queries are hard-bounded;
* whether recovery ran, and why it was skipped, is recorded in the event log and
  in the run summary.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from athena.agents.a2a_stage import DEFAULT_CARD_URL, build_a2a_stage
from athena.agents import bb_io
from athena.agents.lifecycle import close_toolsets
from athena.agents.researcher import build_role_researcher
from athena.agents.routing import classify
from athena.agents.sufficiency import Assessment, assess
from athena.agents.tool_report import build_tool_report
from athena.blackboard import Blackboard
from athena.observability.recorder import get_recorder
from athena.tools import registry

DEFAULT_MAX_ROUNDS = 1
DEFAULT_MAX_QUERIES = 2
MAX_TASK_CHARS = 2400
_QUERY_IN_SUMMARY = re.compile(r"'query':\s*'([^']*)'")

# The query fragment used to target each requested part deterministically.
ASPECT_QUERIES = {
    "mechanism": "working principle mechanism",
    "efficiency": "efficiency specific impulse performance",
    "propellant": "propellant fuel oxidizer",
    "role": "role attitude control spacecraft",
    "advantages": "advantages benefits",
    "disadvantages": "limitations disadvantages",
    "applications": "applications typical use",
    "release": "release notes version",
    "changes": "release notes changes",
    "definition": "definition",
    "activities": "activities scope",
}


def max_rounds() -> int:
    value = (os.getenv("ATHENA_RECOVERY_MAX_ROUNDS") or "").strip()
    return int(value) if value.isdigit() else DEFAULT_MAX_ROUNDS


def max_queries() -> int:
    value = (os.getenv("ATHENA_RECOVERY_MAX_QUERIES") or "").strip()
    return int(value) if value.isdigit() else DEFAULT_MAX_QUERIES


def _normalise_query(text: str) -> str:
    return " ".join((text or "").lower().split())


def attempted_queries(tool_report: dict | None) -> list[tuple[str, bool]]:
    """Every search query already attempted in this run, and whether it worked."""
    found: list[tuple[str, bool]] = []
    for call in (tool_report or {}).get("calls") or []:
        if call.get("tool") not in {"web_search", "rag_search"}:
            continue
        match = _QUERY_IN_SUMMARY.search(str(call.get("summary") or ""))
        if not match:
            continue
        query = match.group(1).strip()
        if query:
            found.append((query, bool(call.get("ok"))))
    return found


def failed_urls(tool_report: dict | None) -> list[str]:
    """URLs whose fetch already failed, so recovery does not retry them."""
    urls: list[str] = []
    for call in (tool_report or {}).get("calls") or []:
        if call.get("tool") != "fetch_page" or call.get("ok"):
            continue
        url = str(call.get("requested_url") or call.get("source_url") or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


@dataclass(frozen=True)
class RecoveryPlan:
    """What is missing, whether to recover, and exactly what to ask for."""

    needed: bool = False
    reasons: tuple[str, ...] = ()
    role: str = ""
    queries: tuple[str, ...] = ()
    skipped_queries: tuple[str, ...] = ()
    failed_urls: tuple[str, ...] = ()
    skip_reason: str = ""
    rounds: int = 0
    max_queries: int = DEFAULT_MAX_QUERIES
    extra: dict[str, Any] = field(default_factory=dict)

    def reason_text(self) -> str:
        """One deterministic sentence for the event log and the summary."""
        if self.needed:
            text = "research recovery: " + "; ".join(self.reasons)
            if self.queries:
                text += " | new queries: " + "; ".join(self.queries)
            if self.skipped_queries:
                text += " | skipped repeated queries: " + "; ".join(self.skipped_queries)
            return text
        return "research recovery skipped: " + (self.skip_reason or "nothing specific is missing")


def _reasons(assessment: Assessment) -> list[str]:
    reasons: list[str] = []
    for subject in assessment.missing_subjects:
        reasons.append(f"no retrieved source covers the subject {subject!r}")
    for aspect in assessment.missing_aspects:
        reasons.append(f"requested part not evidenced: {aspect}")
    for requirement in assessment.missing_requirements:
        reasons.append(f"unestablished requirement: {requirement}")
    for conflict in assessment.conflicts:
        if conflict.missing_accounts:
            reasons.append(
                "no retrieved source supports the "
                + " / ".join(conflict.missing_accounts)
                + " account of the requested conflict"
            )
        elif not conflict.genuine:
            reasons.append(
                "no credible source pair supports the requested conflict ("
                + " / ".join(conflict.accounts)
                + ")"
            )
    return reasons


def _query_subject(assessment: Assessment) -> str:
    if assessment.missing_subjects:
        return " ".join(
            item for item in assessment.missing_subjects
            if item and item != "subject-specific source"
        )
    return assessment.subject or ""


def build_queries(
    assessment: Assessment,
    reasons: list[str] | None = None,
    limit: int = DEFAULT_MAX_QUERIES,
) -> list[str]:
    """Deterministic queries aimed at the specific gaps."""
    subject = _query_subject(assessment)
    queries: list[str] = []
    for aspect in assessment.missing_aspects:
        if aspect.startswith("conflicting account:"):
            account = aspect.split(":", 1)[1].strip()
            queries.append(f"{subject} {account}".strip())
            continue
        fragment = ASPECT_QUERIES.get(aspect, aspect)
        queries.append(f"{subject} {fragment}".strip())
    if assessment.missing_requirements:
        queries.append(f"{subject} latest stable version release notes".strip())
    for conflict in assessment.conflicts:
        if conflict.genuine:
            continue
        queries.append(f"{subject} {' vs '.join(conflict.accounts)}".strip())
    if not queries and reasons:
        queries.append(f"{subject} official documentation".strip())
    clean: list[str] = []
    for query in queries:
        query = " ".join(query.split())
        if query and _normalise_query(query) not in {
            _normalise_query(item) for item in clean
        }:
            clean.append(query)
    return clean[:limit]


def plan_recovery(
    question: str,
    assessment: Assessment,
    tool_report: dict | None = None,
    available_roles=(),
    rounds_used: int = 0,
) -> RecoveryPlan:
    """Decide whether one targeted recovery round is justified, and for what."""
    reasons = _reasons(assessment)
    limit = max_queries()
    rounds = max_rounds()
    if not reasons:
        return RecoveryPlan(
            skip_reason="the evidence covers every requested subject and part",
            rounds=rounds_used,
            max_queries=limit,
        )
    if rounds_used >= rounds:
        return RecoveryPlan(
            reasons=tuple(reasons)[:6],
            skip_reason=f"recovery round limit ({rounds}) reached",
            rounds=rounds_used,
            max_queries=limit,
        )
    available = tuple(available_roles or ())
    if not available:
        return RecoveryPlan(
            reasons=tuple(reasons)[:6],
            skip_reason="no research worker is enabled for this run",
            rounds=rounds_used,
            max_queries=limit,
        )
    route = classify(question)
    if route.kind == "local" and "rag_researcher" in available:
        role = "rag_researcher"
    elif "web_researcher" in available:
        role = "web_researcher"
    elif "rag_researcher" in available:
        role = "rag_researcher"
    else:
        return RecoveryPlan(
            reasons=tuple(reasons)[:6],
            skip_reason="no web or local research worker is enabled for this run",
            rounds=rounds_used,
            max_queries=limit,
        )

    seen = {_normalise_query(query) for query, _ok in attempted_queries(tool_report)}
    candidates = build_queries(assessment, reasons, limit=limit * 3)
    skipped: list[str] = []
    queries: list[str] = []
    for query in candidates:
        if _normalise_query(query) in seen:
            skipped.append(query)
            continue
        queries.append(query)
    queries = queries[:limit]
    if not queries:
        return RecoveryPlan(
            reasons=tuple(reasons)[:6],
            role=role,
            skipped_queries=tuple(skipped[:4]),
            failed_urls=tuple(failed_urls(tool_report)[:4]),
            skip_reason="every targeted query was already attempted",
            rounds=rounds_used,
            max_queries=limit,
        )
    return RecoveryPlan(
        needed=True,
        reasons=tuple(reasons)[:6],
        role=role,
        queries=tuple(queries),
        skipped_queries=tuple(skipped[:4]),
        failed_urls=tuple(failed_urls(tool_report)[:4]),
        rounds=rounds_used + 1,
        max_queries=limit,
    )


def recovery_task_text(
    blackboard: Blackboard,
    plan: RecoveryPlan,
    assessment: Assessment,
) -> str:
    """The recovery worker's task: only the gaps, and what not to repeat."""
    lines = [
        bb_io.task_text(blackboard),
        "",
        "MISSING SUBJECTS: " + (", ".join(assessment.missing_subjects) or "(none)"),
        "MISSING ANSWER PARTS: " + (", ".join(assessment.missing_aspects) or "(none)"),
        "UNESTABLISHED REQUIREMENTS: "
        + ("; ".join(assessment.missing_requirements) or "(none)"),
    ]
    for conflict in assessment.conflicts:
        lines.append("CONFLICT: " + conflict.note)
    lines += [
        "BLOCKING ISSUES: " + ("; ".join(assessment.issues) or "(none)"),
        "WHY THIS RECOVERY ROUND EXISTS: " + "; ".join(plan.reasons),
        "SEARCH EXACTLY THESE TARGETED QUERIES (do not repeat earlier ones):",
    ]
    lines += [f"- {query}" for query in plan.queries]
    if plan.skipped_queries:
        lines.append(
            "QUERIES ALREADY ATTEMPTED (do not repeat): "
            + "; ".join(plan.skipped_queries)
        )
    if plan.failed_urls:
        lines.append(
            "URLS THAT ALREADY FAILED (do not retry): " + ", ".join(plan.failed_urls)
        )
    lines += [
        "CRITIC GAPS:",
        bb_io.critique_text(blackboard) or "(none)",
        "",
        f"At most {plan.max_queries} new queries and one round. Target only these "
        "gaps with new primary evidence. Preserve the evidence already recorded and "
        "do not repeat successful calls.",
    ]
    return "\n".join(lines)[:MAX_TASK_CHARS]


class RecoveryReviewAgent(BaseAgent):
    """Run at most one targeted research pass and then re-review the report."""

    run_id: str
    card_url: str = DEFAULT_CARD_URL
    store: Any = None

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        blackboard = Blackboard.open(self.run_id, self.store)
        task = bb_io.section(blackboard, "task")
        question = task.question if task else ""
        selection = registry.task_selection(self.run_id, self.store)
        tool_report = build_tool_report(self.run_id, self.store)
        assessment = assess(
            question,
            bb_io.load_evidence(blackboard, limit=None),
            enabled_tools=task.tools if task else None,
            tool_report=tool_report,
        )
        critique = bb_io.section(blackboard, "critique")
        plan = plan_recovery(
            question,
            assessment,
            tool_report=tool_report,
            available_roles=registry.available_roles(selection),
        )
        prose_revision = bool(
            critique is not None
            and not critique.approved
            and critique.severity == "high"
        )
        if not plan.needed and not prose_revision:
            self._note(plan)
            yield Event(
                author=self.name,
                custom_metadata={
                    "recovery_rounds": 0,
                    "recovery_skipped_reason": plan.skip_reason,
                },
            )
            return

        if plan.needed:
            recovery_task = recovery_task_text(blackboard, plan, assessment)
            self._note(plan)
            worker = build_role_researcher(
                plan.role, self.run_id, self.store, task_text=recovery_task
            )
            try:
                async for event in worker.run_async(ctx):
                    yield event
            finally:
                # The worker owns an MCP stdio toolset: release it even when the
                # stage is cancelled, so no tool-server subprocess is orphaned.
                await close_toolsets(worker)
        else:
            self._note(plan)

        if plan.needed or prose_revision:
            writer = build_a2a_stage("writer", self.run_id, self.card_url, self.store)
            critic = build_a2a_stage("critic", self.run_id, self.card_url, self.store)
            async for event in writer.run_async(ctx):
                yield event
            async for event in critic.run_async(ctx):
                yield event

        yield Event(
            author=self.name,
            custom_metadata={
                "recovery_rounds": 1 if plan.needed else 0,
                "recovery_reason": plan.reason_text() if plan.needed else "",
                "recovery_skipped_reason": plan.skip_reason,
            },
        )

    def _note(self, plan: RecoveryPlan) -> None:
        recorder = get_recorder()
        if recorder is None:
            return
        recorder.emit(
            "recovery",
            "supervisor",
            "adk",
            payload_summary=plan.reason_text()[:400],
            extra={
                "skipped": not plan.needed,
                "role": plan.role,
                "reason": "; ".join(plan.reasons) if plan.reasons else plan.skip_reason,
                "queries": list(plan.queries),
                "skipped_queries": list(plan.skipped_queries),
                "rounds": plan.rounds,
            },
        )


def build_recovery_review(
    run_id: str, store: Any = None, card_url: str = DEFAULT_CARD_URL
) -> RecoveryReviewAgent:
    return RecoveryReviewAgent(
        name="recovery_review",
        description="Targeted evidence recovery after the flat review.",
        run_id=run_id,
        card_url=card_url,
        store=store,
    )
