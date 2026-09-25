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
    assert asdict(paused)["paused_by"] == "owner"
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    report = next(tool for tool in TOOLS if tool.name == "comms_goal")
    with pytest.raises(ValueError, match="paused by the owner.*Do not resume"):
        report.invoke(reopened, {"goal_id": goal.id, "status": "active", "progress": "4/50"})
    resume = next(tool for tool in TOOLS if tool.name == "comms_resume_goal")
    with pytest.raises(ValueError, match="paused by the owner.*Do not resume"):
        resume.invoke(reopened, {"goal_id": goal.id, "progress": "I should continue"})
    assert reopened.registry.require("worker").goal == paused
    active = reopened.update_goal("worker", "active", goal_id=goal.id, owner_action=True)
    assert active.active and active.paused_by is None


def test_model_pause_and_legacy_pause_do_not_claim_owner_action(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.update_goal("worker", "set", text="Read fifty files")
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    result = comms.update_goal("worker", "paused", goal_id=goal.id, model_report=True)
    assert result.paused_by == "model"
    from agent_comms import Goal

    assert Goal("legacy", "id", status="paused").paused_by is None
