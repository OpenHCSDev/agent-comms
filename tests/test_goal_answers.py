"""Goal absence is dispatched before work; a clear never revives a turn's goal."""

import os
from contextlib import ExitStack, contextmanager
from uuid import uuid4

import pytest

from agent_comms import agent_events as events
from agent_comms.child_process import ProcessIdentity
from agent_comms.goal_actions import ClearGoalAction, SetGoalAction
from agent_comms.goal_attempts import ProviderUsageTotal
from agent_comms.goal_turn_permit import NoLaunchPermit
from agent_comms.goals import NoGoal
from agent_comms.pi_payloads import PiUsage
from agent_comms.thread_identity import RegisteredTurn
from agent_comms.turn_goal_account import TurnGoalAccount
from agent_comms.turn_terminal import EndedWithoutResult, OperatorStopped
from agent_comms.threads import Thread


@contextmanager
def goal_turn(comms, original_goal):
    comms.registry.declare(Thread(
        "worker", frozenset(), str(comms.root.parent),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    if original_goal:
        goal = comms.goals.update_goal("worker", SetGoalAction(text="Original work"))
    turn = RegisteredTurn(uuid4().hex)
    owner = comms.agents.begin_turn("worker", turn.value)
    lease = owner.thread.require_turn_lease()
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
        account.finish(terminal)
        assert comms.registry.require("worker").goal == NoGoal()
        assert comms.goals.open_goal_store().snapshot(goal.id).lifecycle.successors() == ()
        assert not account.pending_origins


@pytest.mark.parametrize("original_goal", [False, True])
@pytest.mark.parametrize("clear_goal", [False, True])
def test_provider_usage_follows_the_declared_goal(copied_goal_bus, original_goal, clear_goal):
    comms = copied_goal_bus
    usage = PiUsage.from_wire({"input": 3, "output": 2, "totalTokens": 5})
    with goal_turn(comms, original_goal) as account:
        goal = comms.registry.require("worker").goal.require()
        if clear_goal:
            comms.goals.update_goal("worker", ClearGoalAction())
        account.provider_usage(events.ProviderUsage("response", usage))
        total = ProviderUsageTotal.for_goal(comms.goals.open_goal_store(), goal.id)
        assert total.responses == (0 if clear_goal else 1)
        assert account.unattributed_usage == ([("response", usage)] if clear_goal else [])


def test_absent_goal_contains_no_actual_goals():
    assert tuple(NoGoal()) == ()
