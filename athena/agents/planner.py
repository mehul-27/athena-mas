"""planner: an ADK LlmAgent that turns the task into a short plan.

The model decides the steps; a small tool writes them to the blackboard as a
``Plan`` under author ``planner``. Keeping the write in a tool means the plan is
structured and validated, never a paragraph the supervisor has to parse.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from athena.agents import bb_io
from athena.blackboard import Blackboard, BlackboardStore, Plan, PlanStep
from athena.config.llm import get_adk_model

MAX_STEPS = 4
OWNERS = {"researcher", "analyst", "writer"}

INSTRUCTION = """You are the planner in a research pipeline.

Read the question, then call `write_plan` exactly once with 2-4 short steps.
Each step must be a string in the form "<owner>: <what to do>", where <owner>
is one of: researcher (gather evidence), analyst (synthesise), writer (draft
the answer).

Cover the whole job: gather, analyse, write. Do not call the tool twice and do
not answer the question yourself.
"""


def parse_steps(raw_steps: list[str]) -> list[PlanStep]:
    """Turn 'owner: description' strings into validated plan steps."""
    steps: list[PlanStep] = []
    for index, raw in enumerate(raw_steps[:MAX_STEPS], start=1):
        owner, separator, description = str(raw).partition(":")
        if not separator or owner.strip().lower() not in OWNERS:
            owner, description = "researcher", str(raw)
        steps.append(
            PlanStep(
                id=f"s{index}",
                description=description.strip() or "Investigate the question",
                assigned_to=owner.strip().lower(),
            )
        )
    return steps


def build_plan_writer(run_id: str, store: BlackboardStore | None = None):
    def write_plan(steps: list[str]) -> str:
        """Record the research plan. Each step is "<owner>: <what to do>".

        Args:
            steps: 2-4 steps, each formatted as "owner: description".
        """
        plan = Plan(steps=parse_steps(steps))
        version = Blackboard.open(run_id, store).write("plan", plan, author="planner")
        return f"Plan with {len(plan.steps)} steps recorded at version {version}."

    return write_plan


def build_planner(run_id: str, store: BlackboardStore | None = None) -> LlmAgent:
    """The planner agent, bound to one run."""
    blackboard = Blackboard.open(run_id, store)
    return LlmAgent(
        name="planner",
        model=get_adk_model("planner"),
        description="Decomposes the question into a short plan.",
        instruction=f"{INSTRUCTION}\nThe task:\n{bb_io.task_text(blackboard)}\n",
        tools=[build_plan_writer(run_id, store)],
    )
