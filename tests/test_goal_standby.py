"""Declared goal dependencies control scheduling without changing legacy registry rows."""

import asyncio
import json
from dataclasses import replace

import pytest

from agent_comms import GoalExecution, GoalExecutionState, Thread
from agent_comms.acp import CommsAgent
from agent_comms.declarations import _store_lock
from agent_comms.goal_attempts import GoalAttemptStore, StaleAttempt
from agent_comms.operations import wire
from agent_comms.tools import TOOLS


@pytest.mark.parametrize("wake", ["child", "owner", "revoked"])
async def test_standby_waits_for_declared_identity_and_preserves_goal_authority(
    tmp_path, monkeypatch, wake
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    monkeypatch.setenv("PI_AGENT_ID", "parent")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "parent"))
    comms.register(Thread("child", frozenset(), str(tmp_path)))
    comms.register(Thread("other", frozenset(), str(tmp_path)))
    store = agent._open_goal_store()
    goal = comms.update_goal("parent", "set", text="Review @child work", owner_store=store)
    report = next(tool for tool in TOOLS if tool.name == "comms_goal")
    calls = []
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"].model_dump(by_alias=True))

    agent.on_connect(Client())

    async def events(*args, **kwargs):
        calls.append(args[2])
        native_id = f"{len(calls):032x}"
        with kwargs["send_boundary"](None, native_id, args[2]) as allowed:
            assert allowed is True
        assert kwargs["native_start"](None, native_id, args[2])
        yield {"type": "input_started", "id": None}
        if len(calls) == 1:
            result = report.invoke(
                comms,
                {
                    "goal_id": goal.id,
                    "status": "standby",
                    "progress": "Waiting for review",
                    "wait_for": ["@child"],
                },
            )
            yield {
                "type": "tool_end",
                "id": "wait",
                "name": "comms_goal",
                "ok": True,
                "output": json.dumps(result),
            }
        yield {"type": "settled"}
        yield {"type": "done", "ok": True, "text": "Waiting" if len(calls) == 1 else "Received"}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_agent_turn("parent", "parent", "Delegate work", autonomous_goal=True)
        current = comms.registry.require("parent").goal
        assert current.active and current.text == "Review @child work"
        wait = comms.goal_wait("parent")
        assert wait is not None
        execution = comms.goal_execution("parent")
        assert execution.state is GoalExecutionState.STANDBY
        assert (
            GoalExecution.from_wire(
                json.loads(
                    json.dumps(agent._session_metadata("parent")["agentComms"]["goalExecution"])
                )
            )
            == execution
        )
        assert any(
            ((row.get("_meta") or {}).get("agentComms", {}).get("goalExecution") or {}).get("state")
            == "standby"
            for row in updates
        )
        view = next(view for view in comms.thread_views() if view.thread.name == "parent")
        assert view.presentation.summary == "Standby · waiting for @child"
        assert store.snapshot(goal.id).number == 2
        agent._schedule_goal("parent")
        assert not agent._pending_turns.get("parent")

        edited = comms.update_goal(
            "parent", "edit", text="Review @child thoroughly", expected_goal=current
        )
        assert edited.id == goal.id and edited.revision == current.revision + 1
        assert comms.goal_wait("parent") == wait
        unrelated = comms.send_message("other", "parent", "Unrelated message")
        await agent._drain_inbox("parent")
        assert not agent._wake_tasks.get("parent")
        assert agent._dispositions.status(f"bus:{unrelated.seq}") == "unknown"
        assert store.snapshot(goal.id).number == 2

        if wake == "owner":
            await agent._run_owned_input("parent", "parent", "New owner instruction")
        else:
            comms.registry.rename("child", "renamed-child")
            message = comms.send_message("renamed-child", "parent", "Implementation ready")
            if wake == "revoked":
                monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
            await agent._drain_inbox("parent")
            if wake == "revoked":
                comms.update_goal("parent", "paused", goal_id=goal.id, owner_action=True)
                CommsAgent._schedule_wake(agent, "parent")
            await asyncio.wait_for(agent._wake_tasks["parent"], timeout=2)
            assert agent._dispositions.status(f"bus:{message.seq}") == (
                "unknown" if wake == "revoked" else "started"
            )
        assert len(calls) == (1 if wake == "revoked" else 2)
        assert store.snapshot(goal.id).number == (2 if wake == "revoked" else 3)
        assert comms.goal_wait("parent") is None
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("changed", ["admission", "pid"])
async def test_ready_recovery_rechecks_executing_owner_before_rotating(
    tmp_path, monkeypatch, changed
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi")
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "parent"))
    store = agent._open_goal_store()
    goal = comms.update_goal("parent", "set", text="Work", owner_store=store)
    owner = comms.registry.require("parent")
    admission = comms.registry.snapshot().admission_generations["parent"]
    if changed == "admission":
        admission += 1
    else:
        owner = replace(owner, pid=owner.pid + 1)
    old_grant = store.ready_grant(goal.id, 1)
    try:
        with _store_lock(comms._wire_lock_path), pytest.raises(StaleAttempt, match="owner changed"):
            agent._ready_goal_grant_locked(
                owner, admission, GoalAttemptStore(store.root), store.snapshot(goal.id)
            )
        assert store.ready_grant(goal.id, 1) == old_grant
    finally:
        await agent.shutdown()


def test_edit_preserves_owner_pause_and_standby_requires_declared_targets(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("parent", frozenset(), str(tmp_path)))
    goal = comms.update_goal("parent", "set", text="Goal with @mention")
    with pytest.raises(ValueError, match="wait_for"):
        comms.update_goal("parent", "standby", goal_id=goal.id)
    comms.update_goal("parent", "paused", owner_action=True)
    comms.update_goal("parent", "edit", text="Edited @mention")
    assert comms.goal_pause("parent").source == "owner"


@pytest.mark.parametrize("already_drained", [False, True])
async def test_standby_refuses_reply_that_arrived_before_wait(
    tmp_path, monkeypatch, already_drained
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "parent"))
    comms.register(Thread("child", frozenset(), str(tmp_path)))
    goal = comms.update_goal("parent", "set", text="Delegate work")
    message = comms.send_message("child", "parent", "Finished immediately")
    try:
        if already_drained:
            await agent._drain_inbox("parent")
            assert agent._dispositions.status(f"bus:{message.seq}") == "unknown"
        with pytest.raises(ValueError, match=f"Dependency reply {message.seq}.*pending or UNKNOWN"):
            comms.update_goal("parent", "standby", goal_id=goal.id, wait_for=["child"])
        assert comms.goal_wait("parent") is None
        assert comms.registry.require("parent").goal == goal
        assert not agent._pending_turns.get("parent")
    finally:
        await agent.shutdown()
