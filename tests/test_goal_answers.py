"""Goal absence is dispatched before work; a clear never revives a turn's goal."""

import os
import shutil
from contextlib import ExitStack, contextmanager
from uuid import uuid4

import pytest

from agent_comms import agent_events as events
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.goal_actions import BlockedGoalAction, ClearGoalAction, SetGoalAction
from agent_comms.goal_attempts import ProviderUsageTotal
from agent_comms.goal_turn_permit import NoLaunchPermit
from agent_comms.goal_states import BlockedGoal
from agent_comms.goals import NoGoal
from agent_comms.pi_payloads import PiUsage
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
def worker_turn(comms):
    turn = TurnId(uuid4().hex)
    owner = comms.agents.begin_turn("worker", turn.value)
    lease = owner.thread.require_turn_lease()
    try:
        with ExitStack() as claims:
            permit = NoLaunchPermit()
            for goal in owner.thread.goal.active():
                permit = TurnGoalAccount.reserve(
                    owner=owner.thread, goal=goal, comms=comms, autonomous=False,
                )
            account = TurnGoalAccount(
                comms=comms, owner=owner.thread, turn=turn, lease=lease,
                permit=permit, pending_origins={}, claims=claims,
            )
            yield account
    finally:
        comms.agents.finish_turn(lease)


@contextmanager
def goal_turn(comms, original_goal):
    comms.registry.declare(comms.registry.new_thread(
        "worker", frozenset(), str(comms.root.parent),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    if original_goal:
        comms.goals.update_goal("worker", SetGoalAction(text="Original work"))
    with worker_turn(comms) as account:
        if not original_goal:
            comms.goals.update_goal("worker", SetGoalAction(text="Work created in turn"))
            account.tool_ended(events.ToolEnd("set", "comms_set_goal", True, "created"))
        yield account


def test_failed_goal_turn_writes_one_block_naming_turn(copied_goal_bus):
    comms = copied_goal_bus
    with goal_turn(comms, True) as account:
        terminal = events.Done("backend failed", False)
        terminal.observe_goal(account)
        account.finish(terminal)
        blocks = [row.after.require().state for row in comms.goals.goal_history("worker")
                  if isinstance(row.after.require().state, BlockedGoal)]
        assert blocks == [BlockedGoal(terminal.failure_reason.value, turn=account.turn)]


def test_later_verified_turn_supersedes_failed_turn_block(copied_goal_bus):
    comms = copied_goal_bus
    with goal_turn(comms, True) as failed:
        failed.finish(events.Done("backend failed", False))
        blocked = comms.registry.require("worker").goal.require()
    with worker_turn(comms) as later:
        assert later.turn != failed.turn
        later.tool_ended(events.ToolEnd("work", "read", True, "read source"))
        later.finish(events.Done("", True))
        active = Comms(comms.root).registry.require("worker").goal.require()
        assert active.id == blocked.id
        assert active.state.active
        assert active.revision == blocked.revision + 1
        assert active.progress == blocked.progress


def test_goal_action_block_survives_later_verified_turn(copied_goal_bus):
    comms = copied_goal_bus
    with goal_turn(comms, True) as first:
        first.finish(events.Done("first step", True))
    blocked = comms.goals.update_goal("worker", BlockedGoalAction(progress="Need owner decision"))
    with worker_turn(comms) as later:
        later.tool_ended(events.ToolEnd("work", "read", True, "read source"))
        later.finish(events.Done("", True))
    assert Comms(comms.root).registry.require("worker").goal == blocked
    assert blocked.state.turn is None


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
