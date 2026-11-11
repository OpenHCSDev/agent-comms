"""Owner pause remains attributable and cannot be undone by a model tool."""


import pytest

from agent_comms.comms import wire
from agent_comms.planning.goal_actions import (
    ActiveGoalAction,
    GoalPrecondition,
    ModelInvocable,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.planning.goal_scope import OneGoal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.request import ToolRequest
from agent_comms.transport.tool_caller import ThreadCaller


def test_owner_pause_survives_reopen_and_explains_stale_model_report(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(NativeThread(name='worker', tags=frozenset(), worktree=Checkout(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Read fifty files"))
    comms.goals.update_goal(
        "worker", PausedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(goal.id))), actor=OwnerInvocable
    )
    reopened = wire(tmp_path)
    paused = reopened.registry.require("worker").goal
    assert paused.state.declared_name == "paused"
    assert reopened.registry.require("worker").goal.state.pause_source.declared_name == "owner"
    assert reopened.views.list_threads()[0].thread.goal.state.pause_source.declared_name == "owner"
    with pytest.raises(ValueError):
        ToolRequest.member_named("comms_goal").invoke(reopened, {"goal_id": goal.id, "status": "active", "progress": "4/50"}, caller=ThreadCaller("worker"))
    with pytest.raises(ValueError):
        ToolRequest.member_named("comms_resume_goal").invoke(reopened, {"goal_id": goal.id, "progress": "I should continue"}, caller=ThreadCaller("worker"))
    assert reopened.registry.require("worker").goal == paused
    active = reopened.goals.update_goal(
        "worker", ActiveGoalAction(expect=GoalPrecondition(goal_id=OneGoal(goal.id))), actor=OwnerInvocable
    )
    active.require_active()
    assert reopened.registry.require('worker').goal.state.pause_source is None


def test_model_cannot_pause(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(NativeThread(name='worker', tags=frozenset(), worktree=Checkout(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Read fifty files"))
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "worker",
            PausedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(goal.id))),
            actor=ModelInvocable,
        )
