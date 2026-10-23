"""Owner pause remains attributable and cannot be undone by a model tool."""

from agent_comms.bus import Bus

import pytest

from agent_comms.comms import wire
from agent_comms.goal_actions import (
    ActiveGoalAction,
    GoalPrecondition,
    ModelInvocable,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.tools import ToolRequest
from agent_comms.tool_caller import ThreadCaller
from agent_comms.threads import Thread


def test_owner_pause_survives_reopen_and_explains_stale_model_report(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    Bus.install(Bus.path_in(comms.root))
    comms.registry.declare(Thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Read fifty files"))
    comms.goals.update_goal(
        "worker", PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    reopened = wire(tmp_path)
    paused = reopened.registry.require("worker").goal
    assert paused.state.declared_name == "paused"
    assert reopened.registry.require("worker").goal.state.pause_source.declared_name == "owner"
    assert "paused_by" not in paused.to_wire()
    assert reopened.views.list_threads()[0].thread.goal.state.pause_source.declared_name == "owner"
    monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
    report = next(
        tool for tool in ToolRequest.members_with(ToolRequest) if tool.declared_name == "comms_goal"
    )
    with pytest.raises(ValueError):
        report.invoke(reopened, {"goal_id": goal.id, "status": "active", "progress": "4/50"}, caller=ThreadCaller("worker"))
    resume = next(
        tool
        for tool in ToolRequest.members_with(ToolRequest)
        if tool.declared_name == "comms_resume_goal"
    )
    with pytest.raises(ValueError):
        resume.invoke(reopened, {"goal_id": goal.id, "progress": "I should continue"}, caller=ThreadCaller("worker"))
    assert reopened.registry.require("worker").goal == paused
    active = reopened.goals.update_goal(
        "worker", ActiveGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    active.require_active()
    assert reopened.registry.require('worker').goal.state.pause_source is None


def test_model_cannot_pause(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    Bus.install(Bus.path_in(comms.root))
    comms.registry.declare(Thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Read fifty files"))
    monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "worker",
            PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
            actor=ModelInvocable,
        )
