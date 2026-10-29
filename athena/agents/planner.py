"""planner: an ADK LlmAgent that turns the task into a short plan.

The model decides the steps; a small tool writes them to the blackboard as a
``Plan`` under author ``planner``. Keeping the write in a tool means the plan is
structured and validated, never a paragraph the supervisor has to parse.
"""

from __future__ import annotations

import json
import re

from google.adk.agents import LlmAgent

from athena.agents import bb_io
from athena.agents.requirements import finding_requirement
from athena.agents.routing import ensure_step_texts, source_guidance
from athena.settings.preferences import with_preferences
from athena.blackboard import Blackboard, BlackboardStore, Plan, PlanStep
from athena.config.llm import get_adk_model

MAX_STEPS = 4
WORKER_ROLES = ("rag_researcher", "web_researcher", "calc_researcher")
DEFAULT_ROLE = "rag_researcher"

INSTRUCTION = """You are the planner in a research pipeline.

Read the question, then call `write_plan` exactly once with 2-4 short steps.
Each step must be a string "<owner>: <what to do>", where <owner> is one of:
- rag_researcher: search the NASA document corpus
- web_researcher: search the web for current information
- calc_researcher: run arithmetic or a forecast over numbers

Use at most one step per owner. Do not call the tool twice and do not answer
the question yourself.
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


def build_plan_writer(
    run_id: str, store: BlackboardStore | None = None, question: str = ""
):
    def write_plan(steps: list[str]) -> str:
        """Record the research plan. Each step is "<owner>: <what to do>".

        Args:
            steps: 2-4 steps, each formatted as "owner: description".
        """
        plan = Plan(steps=parse_steps(ensure_step_texts(steps, question)))
        version = Blackboard.open(run_id, store).write("plan", plan, author="planner")
        return f"Plan with {len(plan.steps)} steps recorded at version {version}."

    return write_plan


def build_planner(
    run_id: str,
    store: BlackboardStore | None = None,
    task_text: str | None = None,
    message_mode: bool = False,
) -> LlmAgent:
    """The planner agent, bound to one run.

    ``message_mode`` (p2p) removes the blackboard tool: the agent must reply
    with the plan as JSON, and the supervisor parses it. That keeps p2p agents
    away from the blackboard entirely.
    """
    text = task_text
    if text is None:
        text = bb_io.task_text(Blackboard.open(run_id, store))

    base = with_preferences(INSTRUCTION)
    base = f"{base}\n{source_guidance(text)}"
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
        tools = [build_plan_writer(run_id, store, text)]

    return LlmAgent(
        name="planner",
        model=get_adk_model("planner"),
        description="Decomposes the question into a short plan.",
        instruction=instruction,
        tools=tools,
    )
