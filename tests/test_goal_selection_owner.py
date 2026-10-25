"""Current goal consumers over the original checked private-bus producer."""

from agent_comms.repo import Checkout
import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.goal_actions import (
    ActiveGoalAction,
    CompletedGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.goal_states import ActiveGoal
from goal_owner_fixture import canonical_goal_wire
from agent_comms.threads import Thread
from agent_comms.goal_scope import OneGoal


@pytest.fixture
def selected_goal(tmp_path):
    comms = canonical_goal_wire(tmp_path / "wire")
    for name in ("worker", "peer", "other"):
        comms.registry.declare(Thread(name, frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Original objective"))
    return comms, goal






def test_exact_cas_and_owner_pause_remain_strict(selected_goal):
    comms, original = selected_goal
    later = comms.goals.update_goal("worker", ActiveGoalAction(progress="Later revision"))
    with pytest.raises(ValueError):
        comms.goals.update_goal("worker", CompletedGoalAction(
            expect=GoalPrecondition(expected_goal=original, goal_id=OneGoal(original.id)),
        ))
    assert comms.registry.require("worker").goal == later
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





