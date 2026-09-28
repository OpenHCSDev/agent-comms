"""Owner pause remains attributable and cannot be undone by a model tool."""

import pytest

from agent_comms import Thread
from agent_comms.goal_actions import (
    ActiveGoalAction,
    GoalPrecondition,
    ModelInvocable,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.operations import wire
from agent_comms.tools import TOOLS


def test_owner_pause_survives_reopen_and_explains_stale_model_report(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.update_goal("worker", SetGoalAction(text="Read fifty files"))
    comms.update_goal(
        "worker", PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    reopened = wire(tmp_path)
    paused = reopened.registry.require("worker").goal
    assert paused.state.declared_name == "paused"
    assert reopened.registry.require("worker").goal.state.pause_source.declared_name == "owner"
    assert "paused_by" not in paused.to_wire()
    assert reopened.list_threads()[0]["goal_pause"]["source"] == "owner"
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    report = next(tool for tool in TOOLS if tool.name == "comms_goal")
    with pytest.raises(ValueError, match="paused by the owner.*Do not resume"):
        report.invoke(reopened, {"goal_id": goal.id, "status": "active", "progress": "4/50"})
    resume = next(tool for tool in TOOLS if tool.name == "comms_resume_goal")
    with pytest.raises(ValueError, match="paused by the owner.*Do not resume"):
        resume.invoke(reopened, {"goal_id": goal.id, "progress": "I should continue"})
    assert reopened.registry.require("worker").goal == paused
    active = reopened.update_goal(
        "worker", ActiveGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    assert (
        active.state.active and reopened.registry.require("worker").goal.state.pause_source is None
    )


def test_model_cannot_pause_and_unattributed_legacy_pause_preserves_owner_stop(
    tmp_path, monkeypatch
):
    comms = wire(tmp_path)
    comms.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.update_goal("worker", SetGoalAction(text="Read fifty files"))
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    with pytest.raises(ValueError, match="actor cannot"):
        comms.update_goal(
            "worker",
            PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
            actor=ModelInvocable,
        )
    from agent_comms import Goal

    saved = Goal.from_wire({"text": "saved objective", "id": "saved", "status": "paused"})
    assert saved.state.source.protects_pause


def test_failed_pause_attribution_cannot_authorize_model_resume(tmp_path, monkeypatch):
    from agent_comms.goal_pauses import GoalPauseEvents

    comms = wire(tmp_path)
    comms.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.update_goal("worker", SetGoalAction(text="Read fifty files"))

    def fail_record(*_args):
        raise OSError("injected attribution write failure")

    monkeypatch.setattr(GoalPauseEvents, "record", fail_record)
    with pytest.raises(OSError, match="injected"):
        comms.update_goal(
            "worker",
            PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
            actor=OwnerInvocable,
        )
    assert comms.registry.require("worker").goal.state.declared_name == "paused"
    assert comms.registry.require("worker").goal.state.pause_source.declared_name == "owner"
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    resume = next(tool for tool in TOOLS if tool.name == "comms_resume_goal")
    with pytest.raises(ValueError, match="paused by the owner"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "Resume anyway"})
