"""Goal absence is dispatched before work; a clear never revives a turn's goal."""

import os
from contextlib import ExitStack
from uuid import uuid4

import pytest

from agent_comms import agent_events as events
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.goal_actions import ClearGoalAction, SetGoalAction
from agent_comms.goal_attempts import NoLaunchPermit
from agent_comms.goals import NoGoal
from agent_comms.thread_identity import TurnId
from agent_comms.turn_goal_account import TurnGoalAccount


@pytest.mark.parametrize("original_goal", [False, True])
@pytest.mark.parametrize("terminal_ok", [False, True, None])
def test_clear_during_original_or_origin_turn_does_not_block_missing_goal(
    tmp_path, original_goal, terminal_ok,
):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread(
        "worker", frozenset(), str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    if original_goal:
        goal = comms.goals.update_goal("worker", SetGoalAction(text="Original work"))
    turn = TurnId(uuid4().hex)
    owner = comms.agents.begin_turn("worker", turn.value)
    lease = owner.thread.turn_lease
    assert lease is not None
    try:
        with ExitStack() as claims:
            permit = (
                TurnGoalAccount.reserve(owner=owner.thread, goal=goal, comms=comms, autonomous=False)
                if original_goal else NoLaunchPermit()
            )
            account = TurnGoalAccount(
                comms=comms, owner=owner.thread, turn=turn, lease=lease,
                permit=permit, claims=claims,
            )
            if not original_goal:
                goal = comms.goals.update_goal("worker", SetGoalAction(text="Work created in turn"))
                account.tool_ended(events.ToolEnd("set", "comms_set_goal", True, "created"))
            comms.goals.update_goal("worker", ClearGoalAction())
            terminal = None if terminal_ok is None else events.Done("finished", terminal_ok)
            if terminal is not None:
                account.done(terminal)
            account.finish(terminal)
            assert comms.registry.require("worker").goal == NoGoal()
            assert comms.goals.open_goal_store().snapshot(goal.id).lifecycle.terminal
    finally:
        comms.agents.finish_turn(lease)


def test_absent_goal_contains_no_actual_goals():
    assert tuple(NoGoal()) == ()
