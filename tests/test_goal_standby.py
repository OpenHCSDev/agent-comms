"""Declared goal dependencies control scheduling without changing legacy registry rows."""

import asyncio
import json
import os
from dataclasses import replace

import pytest

from agent_comms import GoalExecution, GoalExecutionState, Thread
from agent_comms.acp import CommsAgent
from agent_comms.declarations import GoalWaitTarget, _store_lock
from agent_comms.goal_attempts import GoalAttemptStore, StaleAttempt
from agent_comms.goal_waits import GoalWait, GoalWaits
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
    comms.register(Thread("child", frozenset(), str(tmp_path), pid=os.getpid()))
    comms.begin_turn("child", "child-review-in-flight")
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
            assert result["goal"]["status"] == "active"
            assert result["goal_execution"]["state"] == "standby"
            assert [target["name"] for target in result["goal_execution"]["wait_for"]] == ["child"]
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
        # Ordinary nondependency direct DMs now have their own no-goal-permit
        # interrupt route; the tests in test_goal_direct_interrupt cover it.
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


def test_standby_rejects_closed_wait_cycle_while_both_turns_are_active(tmp_path):
    comms = wire(tmp_path)
    for name in ("alice", "bob"):
        comms.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
        comms.begin_turn(name, f"{name}-turn")
    alice = comms.update_goal("alice", "set", text="Wait for Bob")
    bob = comms.update_goal("bob", "set", text="Wait for Alice")
    assert alice is not None and bob is not None

    comms.update_goal("alice", "standby", goal_id=alice.id, wait_for=["bob"])
    with pytest.raises(ValueError, match="dependency wait group.*@alice.*@bob"):
        comms.update_goal("bob", "standby", goal_id=bob.id, wait_for=["alice"])

    assert comms.goal_wait("alice") is not None
    assert comms.goal_wait("bob") is None
    assert comms.registry.require("bob").goal == bob


def test_standby_allows_independent_alternative_to_wait_cycle(tmp_path):
    comms = wire(tmp_path)
    for name in ("alice", "bob", "carol"):
        comms.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
        comms.begin_turn(name, f"{name}-turn")
    alice = comms.update_goal("alice", "set", text="Wait for Bob or Carol")
    bob = comms.update_goal("bob", "set", text="Wait for Alice")
    assert alice is not None and bob is not None

    comms.update_goal("alice", "standby", goal_id=alice.id, wait_for=["bob", "carol"])
    comms.update_goal("bob", "standby", goal_id=bob.id, wait_for=["alice"])

    assert comms.goal_wait("alice") is not None
    assert comms.goal_wait("bob") is not None


def test_idle_active_goal_does_not_make_wait_cycle_runnable(tmp_path):
    comms = wire(tmp_path)
    for name in ("alice", "bob", "carol"):
        comms.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
    for name in ("alice", "bob"):
        comms.begin_turn(name, f"{name}-turn")
    alice = comms.update_goal("alice", "set", text="Wait for Bob or Carol")
    bob = comms.update_goal("bob", "set", text="Wait for Alice")
    carol = comms.update_goal("carol", "set", text="Idle goal")
    assert alice is not None and bob is not None and carol is not None

    comms.update_goal("alice", "standby", goal_id=alice.id, wait_for=["bob", "carol"])
    with pytest.raises(ValueError, match="dependency wait group.*@alice.*@bob"):
        comms.update_goal("bob", "standby", goal_id=bob.id, wait_for=["alice"])

    assert comms.goal_wait("bob") is None
    assert comms.registry.require("bob").goal == bob


def test_dead_active_turn_does_not_make_wait_cycle_runnable(tmp_path):
    comms = wire(tmp_path)
    for name in ("alice", "bob"):
        comms.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
        comms.begin_turn(name, f"{name}-turn")
    comms.register(Thread("carol", frozenset(), str(tmp_path), pid=os.getpid()))
    comms.begin_turn("carol", "carol-turn")
    carol_thread = comms.registry.require("carol")
    assert carol_thread.active_turn is not None
    comms.registry.register(
        replace(
            carol_thread,
            pid=999999999,
            active_turn=replace(carol_thread.active_turn, owner_pid=999999999),
        ),
        comms.registry.status("carol"),
    )
    alice = comms.update_goal("alice", "set", text="Wait for Bob or Carol")
    bob = comms.update_goal("bob", "set", text="Wait for Alice")
    assert alice is not None and bob is not None

    comms.update_goal("alice", "standby", goal_id=alice.id, wait_for=["bob", "carol"])
    with pytest.raises(ValueError, match="dependency wait group.*@alice.*@bob"):
        comms.update_goal("bob", "standby", goal_id=bob.id, wait_for=["alice"])


@pytest.mark.parametrize("pending_reply", [False, True])
def test_liveness_check_releases_preexisting_closed_wait_group(tmp_path, pending_reply):
    comms = wire(tmp_path)
    for name in ("alice", "bob"):
        comms.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
        comms.begin_turn(name, f"{name}-turn")
    alice = comms.update_goal("alice", "set", text="Wait for Bob")
    bob = comms.update_goal("bob", "set", text="Wait for Alice")
    assert alice is not None and bob is not None
    comms.update_goal("alice", "standby", goal_id=alice.id, wait_for=["bob"])

    # Represent a wait recorded by an older owner before cycle admission was
    # enforced. Both turns subsequently finish without any dependency reply.
    owner = comms.registry.require("bob")
    peer = comms.registry.require("alice")
    GoalWaits(tmp_path / "goal_waits.json").record(
        GoalWait(
            bob.id,
            "older-bob-wait",
            bob.revision,
            0,
            (GoalWaitTarget("alice", peer.created_at),),
            owner_created_at=owner.created_at,
        )
    )
    comms.finish_turn("alice", "alice-turn")
    comms.finish_turn("bob", "bob-turn")

    if pending_reply:
        comms.send_message("bob", "alice", "The work is finished")
        assert comms.recover_closed_goal_wait("alice") == ()
        assert comms.goal_wait("alice") is not None
        return

    assert comms.recover_closed_goal_wait("alice") == ("alice", "bob")
    assert comms.goal_wait("alice") is None
    assert comms.goal_execution("alice").state is GoalExecutionState.RUNNABLE
    assert "Standby was released" in comms.registry.require("alice").goal.progress
    assert comms.recover_closed_goal_wait("alice") == ()


def test_recheck_crash_before_wait_clear_keeps_goal_in_standby(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    for name in ("alice", "bob"):
        comms.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
        comms.begin_turn(name, f"{name}-turn")
    alice = comms.update_goal("alice", "set", text="Wait for Bob")
    bob = comms.update_goal("bob", "set", text="Wait for Alice")
    assert alice is not None and bob is not None
    comms.update_goal("alice", "standby", goal_id=alice.id, wait_for=["bob"])
    GoalWaits(tmp_path / "goal_waits.json").record(
        GoalWait(
            bob.id,
            "older-bob-wait",
            bob.revision,
            0,
            (GoalWaitTarget("alice", comms.registry.require("alice").created_at),),
            owner_created_at=comms.registry.require("bob").created_at,
        )
    )
    comms.finish_turn("alice", "alice-turn")
    comms.finish_turn("bob", "bob-turn")
    original_clear = GoalWaits.clear

    def unavailable(*_args, **_kwargs):
        raise OSError("injected wait-clear failure")

    monkeypatch.setattr(GoalWaits, "clear", unavailable)
    with pytest.raises(OSError, match="wait-clear failure"):
        comms.recover_closed_goal_wait("alice")
    reopened = wire(tmp_path)
    assert reopened.registry.require("alice").goal.active
    assert reopened.goal_execution("alice").state is GoalExecutionState.STANDBY
    monkeypatch.setattr(GoalWaits, "clear", original_clear)
    assert reopened.recover_closed_goal_wait("alice") == ("alice", "bob")
    assert reopened.goal_execution("alice").state is GoalExecutionState.RUNNABLE


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
        if already_drained:
            # It is still a fresh ordinary direct DM. That does not turn a
            # pre-wait reply into a qualifying declared dependency receipt.
            pending = agent._pending_turns.get("parent", [])
            assert len(pending) == 1
            assert pending[0].direct_interrupt_goal_id == goal.id
            assert pending[0].goal_wait_id is None
        else:
            assert not agent._pending_turns.get("parent")
    finally:
        await agent.shutdown()
