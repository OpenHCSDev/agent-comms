"""Persistent goal ownership, continuation, and stale-worker protection."""

import asyncio
import os

import pytest

from agent_comms import Thread
from agent_comms.acp import CommsAgent
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

    with pytest.raises(ValueError, match="no longer active"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale duplicate"})
    comms.update_goal("worker", "blocked")
    with pytest.raises(ValueError, match="no longer active"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale blocked update"})
    comms.update_goal("worker", "set", text="Replacement goal")
    with pytest.raises(ValueError, match="replaced"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale replaced goal"})


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


async def test_goal_continues_on_same_owner_and_stops_on_completion(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    await agent.new_session(str(tmp_path / "project"))
    goal = comms.update_goal("project", "set", text="Two steps")
    calls = []
    tool = next(tool for tool in TOOLS if tool.name == "comms_goal")
    monkeypatch.setenv("PI_AGENT_ID", "project")

    async def events(*args, **kwargs):
        calls.append(args[2])
        tool.invoke(
            comms,
            {
                "goal_id": goal.id,
                "status": "active" if len(calls) == 1 else "completed",
                "progress": f"Step {len(calls)} verified",
            },
        )
        yield {"type": "chunk", "text": "Step completed"}
        yield {"type": "settled"}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        for _ in range(2):
            agent._schedule_goal("project")
            await asyncio.wait_for(agent._wake_tasks["project"], timeout=2)
        agent._schedule_goal("project")
        assert len(calls) == 2
        assert all("Two steps" in task and goal.id in task for task in calls)
        assert comms.registry.require("project").goal.status == "completed"
        assert comms.registry.require("project").pid == os.getpid()
        assert len(comms.registry.all_threads()) == 1
        assert comms.full_history() == []
        comms.update_goal("project", "active")
        agent._schedule_goal("project")
        comms.update_goal("project", "paused")
        await asyncio.wait_for(agent._wake_tasks["project"], timeout=2)
        assert len(calls) == 2
    finally:
        await agent.shutdown()


async def test_failed_or_unreported_goal_blocks_instead_of_spinning(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[])
    await agent.new_session(str(tmp_path / "project"))
    comms.update_goal("project", "set", text="Goal needing input")

    async def events(*args, **kwargs):
        yield {"type": "done", "ok": False, "text": "Missing credentials"}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent.prompt("project", [{"type": "text", "text": "continue"}])
        goal = comms.registry.require("project").goal
        assert goal.status == "blocked"
        assert goal.progress == "Backend turn failed; inspect local diagnostics before resuming."
        assert "Missing credentials" not in goal.progress
        comms.update_goal("project", "active")
        await agent.cancel("project")
        assert comms.registry.require("project").goal.status == "paused"
    finally:
        await agent.shutdown()
