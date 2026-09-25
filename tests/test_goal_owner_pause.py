"""Owner pause remains attributable and cannot be undone by a model tool."""

from dataclasses import asdict

import pytest

from agent_comms import Thread
from agent_comms.operations import wire
from agent_comms.tools import TOOLS


def test_owner_pause_survives_reopen_and_explains_stale_model_report(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.update_goal("worker", "set", text="Read fifty files")
    comms.update_goal("worker", "paused", goal_id=goal.id, owner_action=True)
    reopened = wire(tmp_path)
    paused = reopened.registry.require("worker").goal
    assert paused.status == "paused"
    assert reopened.goal_pause("worker").source == "owner"
    assert "paused_by" not in asdict(paused)
    assert reopened.list_threads()[0]["goal_pause"]["source"] == "owner"
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    report = next(tool for tool in TOOLS if tool.name == "comms_goal")
    with pytest.raises(ValueError, match="paused by the owner.*Do not resume"):
        report.invoke(reopened, {"goal_id": goal.id, "status": "active", "progress": "4/50"})
    resume = next(tool for tool in TOOLS if tool.name == "comms_resume_goal")
    with pytest.raises(ValueError, match="paused by the owner.*Do not resume"):
        resume.invoke(reopened, {"goal_id": goal.id, "progress": "I should continue"})
    assert reopened.registry.require("worker").goal == paused
    active = reopened.update_goal("worker", "active", goal_id=goal.id, owner_action=True)
    assert active.active and reopened.goal_pause("worker") is None


def test_model_pause_and_legacy_pause_do_not_claim_owner_action(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.update_goal("worker", "set", text="Read fifty files")
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    comms.update_goal("worker", "paused", goal_id=goal.id, model_report=True)
    assert comms.goal_pause("worker").source == "model"
    from agent_comms import Goal

    assert "paused_by" not in asdict(Goal("legacy", "id", status="paused"))


def test_failed_pause_attribution_cannot_authorize_model_resume(tmp_path, monkeypatch):
    from agent_comms.goal_pauses import GoalPauseEvents

    comms = wire(tmp_path)
    comms.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.update_goal("worker", "set", text="Read fifty files")

    def fail_record(*_args):
        raise OSError("injected attribution write failure")

    monkeypatch.setattr(GoalPauseEvents, "record", fail_record)
    with pytest.raises(OSError, match="injected"):
        comms.update_goal("worker", "paused", goal_id=goal.id, owner_action=True)
    assert comms.registry.require("worker").goal.status == "paused"
    assert comms.goal_pause("worker") is None
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    resume = next(tool for tool in TOOLS if tool.name == "comms_resume_goal")
    with pytest.raises(ValueError, match="owner must resume"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "Resume anyway"})
