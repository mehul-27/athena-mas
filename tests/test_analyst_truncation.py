"""Analyst truncation and role-cap regressions (offline)."""

from __future__ import annotations

import json

import pytest
from instructor.core import IncompleteOutputException

from athena.agents.crew_roles import analyse
from athena.agents.crew_server import run_role_request
from athena.blackboard import Analysis, Blackboard, SQLiteStore, Task
from athena.config.llm import max_tokens_for
from athena.summary import RunSummary, finish, note_ack


@pytest.fixture()
def blackboard(tmp_path) -> Blackboard:
    return Blackboard.create(
        Task(question="Explain three changes in Python 3.13."),
        store=SQLiteStore(tmp_path / "bb.db"),
    )


def test_analyst_cap_has_headroom_for_reasoning_but_does_not_change_critic(
    monkeypatch,
) -> None:
    monkeypatch.delenv("LLM_MAX_TOKENS_ANALYST", raising=False)

    assert max_tokens_for("analyst") == 3000
    assert max_tokens_for("critic") == 3000


def test_truncated_analyst_output_writes_no_analysis_and_degrades(
    blackboard: Blackboard,
) -> None:
    def generate(_prompt: str):
        raise IncompleteOutputException(
            "The output is incomplete due to a max_tokens length limit."
        )

    with pytest.raises(IncompleteOutputException):
        analyse(blackboard.run_id, "analyse", store=blackboard.store, generate=generate)

    assert blackboard.read("analysis") is None, "no placeholder may be written"

    def boom(_run_id: str, _instruction: str, store=None):
        raise IncompleteOutputException(
            "The output is incomplete due to a max_tokens length limit."
        )

    ack = json.loads(
        run_role_request(
            json.dumps({
                "run_id": blackboard.run_id,
                "role": "analyst",
                "instruction": "analyse",
            }),
            roles={"analyst": boom},
            store=blackboard.store,
        )
    )

    summary = RunSummary(pattern="sequential")
    note_ack(summary, json.dumps(ack))
    finish(summary)

    assert ack["ok"] is False
    assert ack["error_type"] == "IncompleteOutputException"
    assert summary.stage_errors == ["analyst: IncompleteOutputException"]
    assert summary.degraded is True
    assert blackboard.read("analysis") is None


def test_successful_analyst_path_still_writes_the_section(blackboard: Blackboard) -> None:
    def generate(_prompt: str):
        return Analysis(
            key_findings=["PEP 703 makes the GIL optional."],
            gaps=["The local corpus has no Python release notes."],
        )

    ack = analyse(
        blackboard.run_id, "analyse", store=blackboard.store, generate=generate
    )

    stored = blackboard.read("analysis")[0]
    assert ack["section"] == "analysis"
    assert ack["version"] == 1
    assert stored.key_findings == ["PEP 703 makes the GIL optional."]
    assert stored.gaps == ["The local corpus has no Python release notes."]
