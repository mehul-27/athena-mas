"""Critic truncation is distinct from schema validation, and neither is approval."""

from __future__ import annotations

import json

import pytest
from instructor.core import IncompleteOutputException
from pydantic import ValidationError

from athena.agents.crew_roles import critique
from athena.agents.crew_server import run_role_request
from athena.blackboard import Blackboard, Critique, SQLiteStore, Task


@pytest.fixture()
def blackboard(tmp_path) -> Blackboard:
    return Blackboard.create(Task(question="q"), store=SQLiteStore(tmp_path / "bb.db"))


def test_incomplete_output_is_an_error_and_writes_no_critique(
    blackboard: Blackboard,
) -> None:
    def generate(_prompt: str):
        raise IncompleteOutputException()

    with pytest.raises(IncompleteOutputException):
        critique(blackboard.run_id, "review", store=blackboard.store, generate=generate)

    assert blackboard.read("critique") is None, "a missing critique is not approval"


def test_schema_validation_error_keeps_its_type(blackboard: Blackboard) -> None:
    try:
        Critique.model_validate({})
    except ValidationError as exc:
        validation_error = exc
    else:  # pragma: no cover - the schema requires severity and approved
        raise AssertionError("Critique validation unexpectedly accepted an empty object")

    def generate(_prompt: str):
        raise validation_error

    with pytest.raises(ValidationError) as caught:
        critique(blackboard.run_id, "review", store=blackboard.store, generate=generate)

    assert not isinstance(caught.value, IncompleteOutputException)
    assert blackboard.read("critique") is None


def test_critic_failure_ack_is_not_an_approval(blackboard: Blackboard) -> None:
    def boom(_run_id: str, _instruction: str, store=None):
        raise IncompleteOutputException()

    ack = json.loads(
        run_role_request(
            json.dumps({
                "run_id": blackboard.run_id,
                "role": "critic",
                "instruction": "review",
            }),
            roles={"critic": boom},
            store=blackboard.store,
        )
    )

    assert ack["ok"] is False
    assert ack["error_type"] == "IncompleteOutputException"
    assert blackboard.read("critique") is None
