"""planner: an ADK LlmAgent that turns the task into a short plan.

The model decides the steps; a small tool writes them to the blackboard as a
``Plan`` under author ``planner``. Keeping the write in a tool means the plan is
structured and validated, never a paragraph the supervisor has to parse.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from google.adk.agents import LlmAgent

from athena.agents import bb_io
from athena.agents.requirements import finding_requirement
from athena.agents.routing import (
    classify,
    ensure_step_texts,
    request_profile,
    source_guidance,
)
from athena.observability.recorder import get_recorder
from athena.settings.preferences import with_preferences
from athena.blackboard import Blackboard, BlackboardStore, Plan, PlanStep
from athena.config.llm import get_adk_model
from athena.tools import registry

MAX_STEPS = 4
WORKER_ROLES = ("rag_researcher", "web_researcher", "calc_researcher")
DEFAULT_ROLE = "rag_researcher"

INSTRUCTION = """You are the planner in a research pipeline.

Read the question, then call `write_plan` exactly once with 2-4 short steps.
Each step must be a string "<owner>: <what to do>", where <owner> is one of:
- rag_researcher: search a supplied local document corpus
- web_researcher: search the web for current information
- calc_researcher: run the numbers, but only when the question supplies numeric
  inputs or explicitly asks for a calculation or forecast

Use at most one step per owner. Do not call the tool twice and do not answer
the question yourself. Do not add a calculation or forecast step merely because
the question mentions a technical object.
"""


def normalise_role(owner: str) -> str:
    """Map whatever the model wrote onto one of the three worker roles."""
    cleaned = (owner or "").strip().lower().replace(" ", "_").replace("-", "_")
    if cleaned in WORKER_ROLES:
        return cleaned
    # Common near-misses the model still produces.
    if "rag" in cleaned or "corpus" in cleaned or "document" in cleaned:
        return "rag_researcher"
    if "web" in cleaned or "internet" in cleaned or "online" in cleaned:
        return "web_researcher"
    if "calc" in cleaned or "math" in cleaned or "forecast" in cleaned:
        return "calc_researcher"
    return DEFAULT_ROLE


def parse_steps(raw_steps: list[str]) -> list[PlanStep]:
    """Turn 'owner: description' strings into validated plan steps."""
    steps: list[PlanStep] = []
    for index, raw in enumerate(raw_steps[:MAX_STEPS], start=1):
        owner, separator, description = str(raw).partition(":")
        if not separator:
            owner, description = DEFAULT_ROLE, str(raw)
        steps.append(
            PlanStep(
                id=f"s{index}",
                description=description.strip() or "Investigate the question",
                assigned_to=normalise_role(owner),
            )
        )
    return steps


def parse_plan_text(text: str) -> list[PlanStep]:
    """Parse a JSON plan block out of a model reply (p2p mode, no tool)."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return parse_steps([text[:200] or "Investigate the question"])
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return parse_steps([text[:200] or "Investigate the question"])
    raw = payload.get("steps", []) if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return parse_steps([str(raw)])
    return parse_steps([str(item) for item in raw])


@dataclass(frozen=True)
class PlanPolicy:
    """A plan after the run's tool selection has been applied."""

    steps: list[PlanStep]
    blocked: list[str]
    limitations: list[str]


def prepare_plan(
    raw_steps: list[str],
    question: str,
    selected: list[str] | tuple[str, ...] | None = None,
) -> PlanPolicy:
    """Validate the planner's steps against the enabled research tools."""
    selection = registry.normalise_selection(selected)
    texts = ensure_step_texts(raw_steps, question, selection)
    parsed = parse_steps(texts)
    profile = request_profile(question)
    allowed: list[PlanStep] = []
    blocked: list[str] = []
    omitted_calculation: list[str] = []
    for step in parsed:
        if step.assigned_to == "calc_researcher" and not profile.allows_calculation:
            omitted_calculation.append(step.description)
            continue
        if registry.role_enabled(step.assigned_to, selection):
            allowed.append(step)
        else:
            blocked.append(f"{step.assigned_to}: {step.description}")

    limitations: list[str] = []
    if omitted_calculation:
        limitations.append(
            "Calculation/forecast steps were omitted because this is a "
            f"{profile.kind} request that does not ask for a numeric result."
        )
    route = classify(question)
    if route.needs_web and not registry.role_enabled("web_researcher", selection):
        limitations.append(
            "Web research is unavailable because web_search and fetch_page are disabled."
        )
    if route.needs_rag and not registry.role_enabled("rag_researcher", selection):
        limitations.append(
            "Local document research is unavailable because rag_search is disabled."
        )
    if not registry.enabled_ids(selection):
        limitations.append("No research tools are enabled, so no evidence can be gathered.")
    if blocked:
        limitations.append(
            "The plan asked for steps this run cannot execute: " + "; ".join(blocked)
        )

    # Renumber so the surviving plan is still a valid, ordered plan.
    steps = [
        step.model_copy(update={"id": f"s{index}"})
        for index, step in enumerate(allowed, start=1)
    ]
    return PlanPolicy(steps, blocked, limitations)


def _visible_selection(run_id: str, store: BlackboardStore | None, selected) -> tuple[str, ...] | None:
    """Explicit selections win; otherwise read the one stored on the task."""
    if selected is not None:
        return registry.normalise_selection(selected)
    return registry.task_selection(run_id, store)


def build_plan_writer(
    run_id: str,
    store: BlackboardStore | None = None,
    question: str = "",
    enabled_tools: list[str] | tuple[str, ...] | None = None,
):
    selection = _visible_selection(run_id, store, enabled_tools)

    def write_plan(steps: list[str]) -> str:
        """Record the research plan. Each step is "<owner>: <what to do>".

        Args:
            steps: 2-4 steps, each formatted as "owner: description".
        """
        policy = prepare_plan(steps, question, selection)
        plan = Plan(steps=policy.steps, blocked=policy.blocked)
        version = Blackboard.open(run_id, store).write("plan", plan, author="planner")
        recorder = get_recorder()
        if recorder is not None:
            for limitation in policy.limitations:
                recorder.emit(
                    "skip",
                    "planner",
                    "adk",
                    payload_summary=limitation,
                    extra={"reason": "tool selection", "blocked": policy.blocked},
                )
        blocked_note = (
            f" {len(policy.blocked)} requested step(s) were blocked by the tool selection."
            if policy.blocked
            else ""
        )
        return (
            f"Plan with {len(plan.steps)} executable step(s) recorded at version "
            f"{version}.{blocked_note}"
        )

    return write_plan


def build_planner(
    run_id: str,
    store: BlackboardStore | None = None,
    task_text: str | None = None,
    message_mode: bool = False,
    enabled_tools: list[str] | tuple[str, ...] | None = None,
) -> LlmAgent:
    """The planner agent, bound to one run.

    ``message_mode`` (p2p) removes the blackboard tool: the agent must reply
    with the plan as JSON, and the supervisor parses it. That keeps p2p agents
    away from the blackboard entirely.
    """
    text = task_text
    if text is None:
        text = bb_io.task_text(Blackboard.open(run_id, store))
    selection = _visible_selection(run_id, store, enabled_tools)

    profile = request_profile(text)
    base = with_preferences(INSTRUCTION)
    base = f"{base}\n{source_guidance(text)}"
    base = f"{base}\nREQUEST PROFILE: {profile.kind}\n{profile.guidance}"
    for note in profile.notes:
        base = f"{base}\n{note}"
    base = f"{base}\n{registry.capability_block(selection)}\n{registry.limit_note(selection)}"
    requirement = finding_requirement(text)
    if requirement:
        base = f"{base}\n{requirement}\nPlan one primary-source investigation per finding."
    if message_mode:
        instruction = (
            f"{base}\nThe task:\n{text}\n\n"
            'Reply with ONLY JSON: {"steps": ["rag_researcher: ...", '
            '"web_researcher: ..."]}'
        )
        tools = []
    else:
        instruction = f"{base}\nThe task:\n{text}\n"
        tools = [build_plan_writer(run_id, store, text, selection)]

    return LlmAgent(
        name="planner",
        model=get_adk_model("planner"),
        description="Decomposes the question into a short plan.",
        instruction=instruction,
        tools=tools,
    )
