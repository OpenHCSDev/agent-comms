"""An owner pause refuses goal input review and a precondition expecting an active goal."""

import os

import pytest

from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.planning.goal_actions import (
    ActiveGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.planning.goal_scope import OneGoal
from agent_comms.planning.goal_states import ActiveGoal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from goal_owner_fixture import canonical_goal_wire


def test_owner_pause_refuses_review_and_active_precondition(tmp_path):
    comms = canonical_goal_wire(tmp_path / "wire")
    for name in ("worker", "peer"):
        comms.registry.declare(NativeThread(name, frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    original = comms.goals.update_goal("worker", SetGoalAction(text="Original objective"))
    paused = comms.goals.update_goal(
        "worker", PausedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(original.id))),
        actor=OwnerInvocable,
    )
    for operation in (
        lambda: comms.goals.goal_input_review("worker", original.id, ["peer"]),
        lambda: comms.goals.update_goal("worker", ActiveGoalAction(
            expect=GoalPrecondition(goal_id=OneGoal(original.id), expected_state=ActiveGoal()),
        )),
    ):
        with pytest.raises(ValueError):
            operation()
        assert comms.registry.require("worker").goal == paused
