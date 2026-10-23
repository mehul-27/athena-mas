"""The sequential baseline: planner -> researcher -> analyst -> writer -> critic."""

from __future__ import annotations

from typing import Any

from athena.agents.a2a_stage import DEFAULT_CARD_URL, build_a2a_stage
from athena.agents.planner import build_planner
from athena.agents.researcher import build_researcher
from athena.agents.supervisor import SupervisorAgent


def build(run_id: str, store: Any = None, card_url: str = DEFAULT_CARD_URL) -> SupervisorAgent:
    return SupervisorAgent(
        name="supervisor",
        description="Sequential pipeline over one blackboard run.",
        run_id=run_id,
        store=store,
        stages=[
            build_planner(run_id, store),
            build_researcher(run_id, store),
            build_a2a_stage("analyst", run_id, card_url, store),
            build_a2a_stage("writer", run_id, card_url, store),
            build_a2a_stage("critic", run_id, card_url, store),
        ],
    )
