"""Persistent goal ownership, continuation, and stale-worker protection."""

import pytest

from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    ActiveGoalAction,
    BlockedGoalAction,
    ClearGoalAction,
    CompletedGoalAction,
    GoalAction,
    GoalPrecondition,
    PausedGoalAction,
    RetryGoalAction,
    SetGoalAction,
)
from agent_comms.goal_states import ActiveGoal, BlockedGoal, CompletedGoal, PausedGoal
from agent_comms.goals import Goal
from agent_comms.threads import Thread
from agent_comms.tools import TOOLS


def test_goal_survives_rename_and_reregistration(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.threads.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Verify the release"))
    comms.threads.set_thread_model("worker", "test/model")
    comms.threads.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    comms.threads.rename_self("renamed")
    restored = wire(tmp_path).registry.require("worker")
    assert restored.goal == goal
    assert restored.model == "test/model"
    assert restored.name == "renamed"
    comms.goals.update_goal("worker", PausedGoalAction())
    tool = next(tool for tool in TOOLS if tool.name == "comms_goal")
    with pytest.raises(ValueError, match="no longer active"):
        tool.invoke(comms, {"goal_id": goal.id, "status": "active", "progress": "late update"})
    replacement = comms.goals.update_goal("worker", SetGoalAction(text="New objective"))
    with pytest.raises(ValueError, match="replaced"):
        comms.goals.update_goal("worker", CompletedGoalAction(expect=GoalPrecondition(goal_id=goal.id)))
    assert comms.registry.require("worker").goal == replacement
    comms.goals.update_goal("worker", ClearGoalAction())
    assert wire(tmp_path).registry.require("worker").goal is None
    with pytest.raises(ValueError, match="No goal"):
        comms.goals.update_goal("worker", ActiveGoalAction())
    with pytest.raises(ValueError, match="requires"):
        comms.goals.update_goal("worker", SetGoalAction(text=" "))
    with pytest.raises(ValueError, match="Unknown"):
        GoalAction.decode("nonsense")


def test_explicit_resume_tool_keeps_goal_id_and_rejects_stale_calls(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.threads.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Finish the release"))
    comms.goals.update_goal("worker", PausedGoalAction(progress="Paused after an uncertain turn"))
    resume = next(tool for tool in TOOLS if tool.name == "comms_resume_goal")

    result = resume.invoke(
        comms, {"goal_id": goal.id, "progress": "Explicitly resumed by the user"}
    )
    assert result["goal"]["id"] == goal.id
    assert FieldCodec.decode(Goal, result["goal"]).state.declared_name == "active"
    assert comms.registry.require("worker").goal.progress == "Explicitly resumed by the user"

    with pytest.raises(ValueError, match="cannot be resumed"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale duplicate"})
    comms.goals.update_goal("worker", BlockedGoalAction(block_reason="Need owner input before retry."))
    with pytest.raises(ValueError, match="cannot be resumed"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale blocked update"})
    comms.goals.update_goal("worker", SetGoalAction(text="Replacement goal"))
    with pytest.raises(ValueError, match="cannot be resumed"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale replaced goal"})


@pytest.mark.parametrize(
    "state,action,label",
    [
        (ActiveGoal(), PausedGoalAction, "Pause"),
        (PausedGoal(), ActiveGoalAction, "Resume"),
        (BlockedGoal("Explicit fixture refusal"), RetryGoalAction, "Retry"),
        (CompletedGoal(), None, "Completed"),
    ],
)
def test_goal_control_distinguishes_resume_retry_and_completion(state, action, label):
    assert state.toggle is action
    assert state.toggle_label == label


def test_agent_can_set_its_own_persistent_goal(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.threads.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    tool = next(tool for tool in TOOLS if tool.name == "comms_set_goal")

    result = tool.invoke(comms, {"text": "Verify the autonomous workflow"})

    goal = comms.registry.require("worker").goal
    assert goal is not None
    assert goal.state.declared_name == "active"
    assert goal.text == "Verify the autonomous workflow"
    assert result["goal"]["id"] == goal.id
