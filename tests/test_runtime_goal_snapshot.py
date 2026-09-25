"""Goal polling and owner actions use a real socket without launching a provider."""

import json
import os
from dataclasses import asdict, replace

import pytest

from agent_comms import Thread, wire
from agent_comms.acp import CommsAgent
from agent_comms.runtime import RuntimeProxy, socket_path

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")


@pytest.fixture
async def goal_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False)
    monkeypatch.setattr(owner, "_ensure_live_drain", lambda _: None)
    session = (await owner.new_session(str(tmp_path / "project"))).session_id
    proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
    scheduled = []
    monkeypatch.setattr(owner, "_schedule_goal", scheduled.append)
    try:
        yield comms, owner, proxy, session, scheduled
    finally:
        await proxy.close()
        await owner.shutdown()


async def test_goal_snapshot_reads_current_pair_without_mutation_or_scheduling(goal_owner):
    comms, owner, proxy, session, scheduled = goal_owner
    assert await proxy.request("goal_snapshot") == {"goal": None, "goalExecution": None}
    comms.register(Thread("child", frozenset(), str(comms.root)))
    goal = comms.update_goal(session, "set", text="Review child output")
    comms.update_goal(session, "standby", goal_id=goal.id, wait_for=["child"])
    expected_goal, expected_execution = comms.goal_snapshot(session)
    before = {p: p.read_bytes() for p in comms.root.rglob("*") if p.is_file()}
    for _ in range(2):
        result = await proxy.request("goal_snapshot")
        assert result == {
            "goal": asdict(expected_goal),
            "goalExecution": json.loads(json.dumps(asdict(expected_execution))),
        }
        assert result["goalExecution"]["state"] == "standby"
    assert {p: p.read_bytes() for p in comms.root.rglob("*") if p.is_file()} == before
    comms.update_goal(session, "paused", goal_id=goal.id, owner_action=True)
    paused = await proxy.request("goal_snapshot")
    assert paused["goal"]["revision"] > result["goal"]["revision"]
    assert paused["goalExecution"]["state"] == "paused"
    assert scheduled == [] and not owner._pending_turns and not owner._wake_tasks


async def test_goal_actions_check_revision_and_preserve_owner_pause(goal_owner):
    comms, owner, proxy, session, scheduled = goal_owner
    goal = comms.update_goal(
        session, "set", text="Review child output", owner_store=owner._open_goal_store()
    )
    paused = await proxy.request(
        "update_goal", status="paused", goal_id=goal.id, expected_revision=goal.revision
    )
    assert paused["goal"]["status"] == paused["goalExecution"]["state"] == "paused"
    assert comms.goal_pause(session).source == "owner"
    assert scheduled == []

    with pytest.raises(RuntimeError, match="changed"):
        await proxy.request(
            "update_goal", status="clear", goal_id=goal.id, expected_revision=goal.revision
        )
    assert await proxy.request("goal_snapshot") == paused
    resumed = await proxy.request(
        "update_goal",
        status="active",
        goal_id=goal.id,
        expected_revision=paused["goal"]["revision"],
    )
    assert resumed["goal"]["status"] == "active"
    assert scheduled == [session]
    assert await proxy.request(
        "update_goal",
        status="clear",
        goal_id=goal.id,
        expected_revision=resumed["goal"]["revision"],
    ) == {"goal": None, "goalExecution": None}
    assert scheduled == [session]
    assert owner._goal_store.snapshot(goal.id).state == "cancelled"


async def test_goal_update_cannot_bypass_blocked_retry_or_replace_owner(goal_owner, monkeypatch):
    comms, owner, proxy, session, scheduled = goal_owner
    goal = comms.update_goal(session, "set", text="Needs review")
    blocked = comms.update_goal(session, "blocked", goal_id=goal.id)
    with pytest.raises(RuntimeError, match="explicit retry"):
        await proxy.request(
            "update_goal", status="active", goal_id=goal.id, expected_revision=blocked.revision
        )
    with pytest.raises(RuntimeError, match="only active, paused, or clear"):
        await proxy.request(
            "update_goal", status="completed", goal_id=goal.id, expected_revision=blocked.revision
        )
    update_goal = comms.update_goal

    def change_owner_before_cas(*args, **kwargs):
        comms.registry.register(replace(comms.registry.require(session), pid=os.getpid() + 100000))
        return update_goal(*args, **kwargs)

    monkeypatch.setattr(comms, "update_goal", change_owner_before_cas)
    with pytest.raises(RuntimeError, match="owner changed"):
        await proxy.request(
            "update_goal", status="clear", goal_id=goal.id, expected_revision=blocked.revision
        )
    assert comms.registry.require(session).goal == blocked
    assert scheduled == []


async def test_goal_update_rechecks_snapshot_inside_write_lock(goal_owner, monkeypatch):
    comms, owner, proxy, session, scheduled = goal_owner
    goal = comms.update_goal(session, "set", text="Original objective")
    update_goal = comms.update_goal
    changed = None

    def edit_before_cas(*args, **kwargs):
        nonlocal changed
        changed = update_goal(session, "edit", text="New objective", goal_id=goal.id)
        return update_goal(*args, **kwargs)

    monkeypatch.setattr(comms, "update_goal", edit_before_cas)
    with pytest.raises(RuntimeError, match="changed"):
        await proxy.request(
            "update_goal", status="paused", goal_id=goal.id, expected_revision=goal.revision
        )
    assert comms.registry.require(session).goal == changed
    assert changed.text == "New objective" and changed.status == "active"
    assert scheduled == []
