"""The tests that would have caught the p2p signature mismatch.

Every cross-module call made through keyword arguments is bound against the real
function signature. Stubs hide this class of bug: a stub accepts anything, so a
call that the real function rejects still passes.
"""

from __future__ import annotations

import inspect
import json

import pytest

from athena.agents import crew_p2p, crew_roles, crew_writer
from athena.agents.crew_server import run_role_request
from athena.blackboard import Blackboard, SQLiteStore, Task

# (callable, kwargs exactly as production passes them)
CALLS = [
    (crew_roles.analyse, {"run_id": "r", "instruction": "i", "store": None}),
    (crew_roles.critique, {"run_id": "r", "instruction": "i", "store": None}),
    (crew_writer.write_report, {"run_id": "r", "instruction": "i", "store": None}),
    (crew_p2p.p2p_analyse,
     {"run_id": "r", "instruction": "i", "store": None, "context": "c"}),
    (crew_p2p.p2p_write_report,
     {"run_id": "r", "instruction": "i", "store": None, "context": "c"}),
    (crew_p2p.p2p_critique,
     {"run_id": "r", "instruction": "i", "store": None, "context": "c"}),
]


@pytest.mark.parametrize("function,kwargs", CALLS, ids=lambda v: getattr(v, "__name__", ""))
def test_production_kwargs_bind_to_the_real_function(function, kwargs) -> None:
    signature = inspect.signature(function)
    signature.bind(**kwargs)  # raises TypeError if an argument is not accepted


def test_p2p_roles_are_a_separate_map() -> None:
    assert set(crew_p2p.P2P_ROLES) == {"analyst", "writer", "critic"}
    assert crew_p2p.P2P_ROLES["analyst"] is crew_p2p.p2p_analyse
    assert crew_p2p.P2P_ROLES["analyst"] is not crew_roles.analyse


def test_p2p_request_routes_to_the_p2p_role(tmp_path) -> None:
    """The executor must not force the blackboard ROLES map on a p2p body."""
    store = SQLiteStore(tmp_path / "bb.db")
    blackboard = Blackboard.create(Task(question="q"), store=store)

    body = json.dumps({"run_id": blackboard.run_id, "role": "analyst",
                       "instruction": "go", "context": "evidence: none"})
    ack = json.loads(run_role_request(body, roles=None, store=store))

    # p2p_analyse ran (it accepts context); a signature error would be ok:false.
    assert ack.get("error_type") != "TypeError"
    assert "unexpected keyword" not in str(ack.get("error", ""))


def test_executor_does_not_default_to_the_blackboard_roles() -> None:
    from athena.agents.crew_server import CrewA2AExecutor

    assert CrewA2AExecutor().roles is None
