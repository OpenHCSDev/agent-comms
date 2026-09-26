"""Persistent goal ownership, continuation, and stale-worker protection."""

import pytest

from agent_comms import Thread
from agent_comms.operations import wire
from agent_comms.tools import TOOLS


def test_goal_survives_rename_and_reregistration(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.update_goal("worker", "set", text="Verify the release")
    comms.set_thread_model("worker", "test/model")
    comms.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    comms.rename_self("renamed")
    restored = wire(tmp_path).registry.require("worker")
    assert restored.goal == goal
    assert restored.model == "test/model"
    assert restored.name == "renamed"
    comms.update_goal("worker", "paused")
    tool = next(tool for tool in TOOLS if tool.name == "comms_goal")
    with pytest.raises(ValueError, match="no longer active"):
        tool.invoke(comms, {"goal_id": goal.id, "status": "active", "progress": "late update"})
    replacement = comms.update_goal("worker", "set", text="New objective")
    with pytest.raises(ValueError, match="replaced"):
        comms.update_goal("worker", "completed", goal_id=goal.id)
    assert comms.registry.require("worker").goal == replacement
    comms.update_goal("worker", "clear")
    assert wire(tmp_path).registry.require("worker").goal is None
    with pytest.raises(ValueError, match="No goal"):
        comms.update_goal("worker", "active")
    with pytest.raises(ValueError, match="requires"):
        comms.update_goal("worker", "set", text=" ")
    with pytest.raises(ValueError, match="Unknown"):
        comms.update_goal("worker", "nonsense")


def test_explicit_resume_tool_keeps_goal_id_and_rejects_stale_calls(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    goal = comms.update_goal("worker", "set", text="Finish the release")
    comms.update_goal("worker", "paused", progress="Paused after an uncertain turn")
    resume = next(tool for tool in TOOLS if tool.name == "comms_resume_goal")

    result = resume.invoke(
        comms, {"goal_id": goal.id, "progress": "Explicitly resumed by the user"}
    )
    assert result["goal"]["id"] == goal.id
    assert result["goal"]["status"] == "active"
    assert comms.registry.require("worker").goal.progress == "Explicitly resumed by the user"

    with pytest.raises(ValueError, match="cannot be resumed"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale duplicate"})
    comms.update_goal("worker", "blocked", block_reason="Need owner input before retry.")
    with pytest.raises(ValueError, match="cannot be resumed"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale blocked update"})
    comms.update_goal("worker", "set", text="Replacement goal")
    with pytest.raises(ValueError, match="cannot be resumed"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale replaced goal"})


def test_goal_control_distinguishes_resume_retry_and_completion():
    from agent_comms import Goal

    assert (
        Goal("work", "id", status="active").toggle_action,
        Goal("work", "id", status="active").toggle_label,
    ) == ("paused", "Pause")
    assert (
        Goal("work", "id", status="paused").toggle_action,
        Goal("work", "id", status="paused").toggle_label,
    ) == ("active", "Resume")
    assert (
        Goal("work", "id", status="blocked").toggle_action,
        Goal("work", "id", status="blocked").toggle_label,
    ) == ("retry", "Retry")
    assert (
        Goal("work", "id", status="completed").toggle_action,
        Goal("work", "id", status="completed").toggle_label,
    ) == ("", "Completed")


def test_agent_can_set_its_own_persistent_goal(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    tool = next(tool for tool in TOOLS if tool.name == "comms_set_goal")

    result = tool.invoke(comms, {"text": "Verify the autonomous workflow"})

    goal = comms.registry.require("worker").goal
    assert goal is not None
    assert goal.status == "active"
    assert goal.text == "Verify the autonomous workflow"
    assert result["goal"]["id"] == goal.id
