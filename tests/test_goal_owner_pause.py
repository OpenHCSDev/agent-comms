"""Owner pause remains attributable and cannot be undone by a model tool."""

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
from agent_comms.threads import Thread
from agent_comms.tools import ToolRequest


def test_owner_pause_survives_reopen_and_explains_stale_model_report(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Read fifty files"))
    comms.goals.update_goal(
        "worker", PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    reopened = wire(tmp_path)
    paused = reopened.registry.require("worker").goal
    assert paused.state.declared_name == "paused"
    assert reopened.registry.require("worker").goal.state.pause_source.declared_name == "owner"
    assert "paused_by" not in paused.to_wire()
    assert reopened.views.list_threads()[0]["goal"]["state"]["source"]["kind"] == "owner"
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    report = next(
        tool for tool in ToolRequest.members_with(ToolRequest) if tool.declared_name == "comms_goal"
    )
    with pytest.raises(ValueError, match="paused by the owner.*Do not resume"):
        report.invoke(reopened, {"goal_id": goal.id, "status": "active", "progress": "4/50"})
    resume = next(
        tool
        for tool in ToolRequest.members_with(ToolRequest)
        if tool.declared_name == "comms_resume_goal"
    )
    with pytest.raises(ValueError, match="paused by the owner.*Do not resume"):
        resume.invoke(reopened, {"goal_id": goal.id, "progress": "I should continue"})
    assert reopened.registry.require("worker").goal == paused
    active = reopened.goals.update_goal(
        "worker", ActiveGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    assert active.state.active and reopened.registry.require("worker").goal.state.pause_source is None


def test_model_cannot_pause(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Read fifty files"))
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    with pytest.raises(ValueError, match="actor cannot"):
        comms.goals.update_goal(
            "worker",
            PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
            actor=ModelInvocable,
        )
