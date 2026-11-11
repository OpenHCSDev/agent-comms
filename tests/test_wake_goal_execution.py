"""Native wake status comes from the real goal execution owner."""

from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.context_segments.wake_values import (
    NoWakeGoalContext,
    WakeGoalContext,
    WakeWorkContext,
)
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.planning.goal_actions import (
    BlockedGoalAction,
    ClearGoalAction,
    CompletedGoalAction,
    GoalAction,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.planning.goal_execution import GoalExecution
from agent_comms.planning.goal_presentation import (
    CompletedExecution,
    PausedExecution,
    RetryableBlockExecution,
    RunnableExecution,
    StandbyExecution,
)
from agent_comms.planning.goals import Goal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from goal_owner_fixture import canonical_goal_wire


@pytest.fixture
def owner(tmp_path: Path) -> Comms:
    comms = canonical_goal_wire(tmp_path)
    for name in ("worker", "dependency"):
        comms.registry.declare(NativeThread(
            name, frozenset(), Checkout(tmp_path),
            process_identity=ProcessIdentity.current(),
        ))
    comms.goals.update_goal("worker", SetGoalAction(text="Finish the release"))
    return comms


def assert_owned_status[Execution: GoalExecution](comms: Comms, expected: type[Execution]) -> Execution:
    thread = comms.registry.require("worker")
    snapshot = comms.goals.goal_snapshot(thread.name)
    context = thread.wake_context(comms.goals)
    encoded = FieldCodec.encode(context)
    assert encoded["current_goal"]["status"] == FieldCodec.encode(snapshot.execution)
    assert FieldCodec.decode(WakeWorkContext, encoded) == context
    assert isinstance(context.current_goal, WakeGoalContext)
    assert isinstance(context.current_goal.execution, expected)
    assert context.current_goal.execution == snapshot.execution
    return context.current_goal.execution


def test_standby_wake_carries_the_wait_and_dependency_identity(owner: Comms) -> None:
    owner.goals.update_goal("worker", StandbyGoalAction(wait_for=("dependency",)))
    status = assert_owned_status(owner, StandbyExecution)
    dependency = owner.registry.require("dependency").incarnation.require_recorded()
    assert tuple(target.incarnation for target in status.wait_for) == (dependency,)
    assert "@dependency" in status.summary
    assert owner.registry.require("worker").goal.state.declared_name == "active"


@pytest.mark.parametrize(("action", "execution"), (
    (SetGoalAction(text="Finish the patch"), RunnableExecution),
    (PausedGoalAction(), PausedExecution),
    (BlockedGoalAction(block_reason="Need a decision"), RetryableBlockExecution),
    (CompletedGoalAction(), CompletedExecution),
))
def test_native_wake_uses_the_effective_execution_family(
    owner: Comms, action: GoalAction[Goal], execution: type[GoalExecution],
) -> None:
    owner.goals.update_goal("worker", action)
    assert_owned_status(owner, execution)


def test_absent_goal_keeps_its_declared_null_context(owner: Comms) -> None:
    owner.goals.update_goal("worker", ClearGoalAction())
    context = owner.registry.require("worker").wake_context(owner.goals)
    assert isinstance(context.current_goal, NoWakeGoalContext)
    assert FieldCodec.encode(context)["current_goal"] is None
