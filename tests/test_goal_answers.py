"""Goal absence is dispatched before work; a clear never revives a turn's goal."""

import os
import shutil
from contextlib import ExitStack, contextmanager
from uuid import uuid4

import pytest

from agent_comms import agent_events as events
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.goal_actions import ClearGoalAction, SetGoalAction
from agent_comms.goal_attempts import NoLaunchPermit
from agent_comms.goal_states import BlockedGoal
from agent_comms.goals import NoGoal
from agent_comms.thread_identity import TurnId
from agent_comms.turn_goal_account import TurnGoalAccount
from agent_comms.turn_terminal import EndedWithoutResult, OperatorStopped


@pytest.fixture
def copied_goal_bus(tmp_path):
    source = Comms(tmp_path / "source")
    source.messaging.initialize_private_initial_protocol()
    target = tmp_path / "copy"
    shutil.copytree(source.root, target)
    return Comms(target)


@contextmanager
def goal_turn(comms, original_goal):
    comms.registry.declare(comms.registry.new_thread(
        "worker", frozenset(), str(comms.root.parent),
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
                permit=permit, pending_origins={}, claims=claims,
            )
            if not original_goal:
                comms.goals.update_goal("worker", SetGoalAction(text="Work created in turn"))
                account.tool_ended(events.ToolEnd("set", "comms_set_goal", True, "created"))
            yield account
    finally:
        comms.agents.finish_turn(lease)


@pytest.mark.parametrize("original_goal", [False, True])
@pytest.mark.parametrize("terminal", [
    events.Done("finished", False), events.Done("finished", True),
    EndedWithoutResult(), OperatorStopped(),
])
def test_clear_during_original_or_origin_turn_does_not_block_missing_goal(
    copied_goal_bus, original_goal, terminal,
):
    comms = copied_goal_bus
    with goal_turn(comms, original_goal) as account:
        goal = comms.registry.require("worker").goal.require()
        comms.goals.update_goal("worker", ClearGoalAction())
        terminal.observe_goal(account)
        account.finish(terminal)
        assert comms.registry.require("worker").goal == NoGoal()
        assert comms.goals.open_goal_store().snapshot(goal.id).lifecycle.terminal
        assert not account.pending_origins


@pytest.mark.parametrize("terminal", [
    EndedWithoutResult(), events.Done("backend failed", False), events.Done("", True),
])
def test_unsuccessful_terminal_still_blocks_goal_on_copied_bus(copied_goal_bus, terminal):
    comms = copied_goal_bus
    with goal_turn(comms, True) as account:
        terminal.observe_goal(account)
        account.finish(terminal)
        state = comms.registry.require("worker").goal.require().state
        assert isinstance(state, BlockedGoal)


def test_absent_goal_contains_no_actual_goals():
    assert tuple(NoGoal()) == ()
