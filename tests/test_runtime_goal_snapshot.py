"""Goal polling and owner actions use a real socket without launching a provider."""

import json
import os
from dataclasses import asdict, replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    BlockedGoalAction,
    EditGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.goal_generation import CancelledGeneration
from agent_comms.goals import Goal
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")


@pytest.fixture
async def goal_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    owner = canonical_agent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False)
    monkeypatch.setattr(owner.inputs, "ensure_live_drain", lambda _: None)
    session = (await owner.new_session(str(tmp_path / "project"))).session_id
    proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
    scheduled = []
    monkeypatch.setattr(owner.turns.goals, "schedule_goal", scheduled.append)
    try:
        yield comms, owner, proxy, session, scheduled
    finally:
        await proxy.close()
        await owner.shutdown()


async def test_goal_snapshot_reads_current_pair_without_mutation_or_scheduling(goal_owner):
    comms, owner, proxy, session, scheduled = goal_owner
    assert await proxy.request("goal_snapshot") == {"goal": None, "goalExecution": None}
    comms.registry.declare(Thread("child", frozenset(), str(comms.root), process_identity=ProcessIdentity.capture(os.getpid())))
    comms.agents.begin_turn("child", "child-work-in-flight")
    goal = comms.goals.update_goal(session, SetGoalAction(text="Review child output"))
    comms.goals.update_goal(
        session, StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("child",))
    )
    expected_goal, expected_execution = comms.goals.goal_snapshot(session)
    before = {p: p.read_bytes() for p in comms.root.rglob("*") if p.is_file()}
    for _ in range(2):
        result = await proxy.request("goal_snapshot")
        assert result == {
            "goal": json.loads(json.dumps(expected_goal.to_wire())),
            "goalExecution": json.loads(json.dumps(asdict(expected_execution))),
        }
        assert result["goalExecution"]["state"] == "standby"
    assert {p: p.read_bytes() for p in comms.root.rglob("*") if p.is_file()} == before
    comms.goals.update_goal(
        session, PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    paused = await proxy.request("goal_snapshot")
    assert paused["goal"]["revision"] > result["goal"]["revision"]
    assert paused["goalExecution"]["state"] == "paused"
    assert scheduled == [] and not owner.inputs.pending_turns and not owner.inputs.wake_tasks


async def test_goal_actions_check_revision_and_preserve_owner_pause(goal_owner):
    comms, owner, proxy, session, scheduled = goal_owner
    goal = comms.goals.update_goal(
        session,
        SetGoalAction(text="Review child output"),
        owner_store=owner.turns.goals.open_goal_store(),
    )
    paused = await proxy.request(
        "update_goal", status="paused", goal_id=goal.id, expected_revision=goal.revision
    )
    assert (
        FieldCodec.decode(Goal, paused["goal"]).state.declared_name
        == paused["goalExecution"]["state"]
        == "paused"
    )
    assert comms.goals.goal_pause(session).source.declared_name == "owner"
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
    assert FieldCodec.decode(Goal, resumed["goal"]).state.declared_name == "active"
    assert scheduled == [session]
    assert await proxy.request(
        "update_goal",
        status="clear",
        goal_id=goal.id,
        expected_revision=resumed["goal"]["revision"],
    ) == {"goal": None, "goalExecution": None}
    assert scheduled == [session]
    assert owner.turns.goals.goal_store.snapshot(goal.id).lifecycle == CancelledGeneration()


async def test_goal_update_cannot_bypass_blocked_retry_or_replace_owner(goal_owner, monkeypatch):
    comms, owner, proxy, session, scheduled = goal_owner
    goal = comms.goals.update_goal(session, SetGoalAction(text="Needs review"))
    blocked = comms.goals.update_goal(
        session,
        BlockedGoalAction(
            expect=GoalPrecondition(goal_id=goal.id),
            block_reason="Unknown prior attempt requires review",
        ),
    )
    with pytest.raises(RuntimeError, match="explicit retry"):
        await proxy.request(
            "update_goal", status="active", goal_id=goal.id, expected_revision=blocked.revision
        )
    with pytest.raises(RuntimeError, match="only active, paused, or clear"):
        await proxy.request(
            "update_goal", status="completed", goal_id=goal.id, expected_revision=blocked.revision
        )
    update_goal = comms.goals.update_goal

    def change_owner_before_cas(*args, **kwargs):
        comms.registry.register(replace(comms.registry.require(session), process_identity=ProcessIdentity(os.getpid() + 100000, 1)))
        return update_goal(*args, **kwargs)

    monkeypatch.setattr(comms.goals, "update_goal", change_owner_before_cas)
    with pytest.raises(RuntimeError, match="owner changed"):
        await proxy.request(
            "update_goal", status="clear", goal_id=goal.id, expected_revision=blocked.revision
        )
    assert comms.registry.require(session).goal == blocked
    assert scheduled == []


async def test_goal_update_rechecks_snapshot_inside_write_lock(goal_owner, monkeypatch):
    comms, owner, proxy, session, scheduled = goal_owner
    goal = comms.goals.update_goal(session, SetGoalAction(text="Original objective"))
    update_goal = comms.goals.update_goal
    changed = None

    def edit_before_cas(*args, **kwargs):
        nonlocal changed
        changed = update_goal(
            session, EditGoalAction(text="New objective", expect=GoalPrecondition(goal_id=goal.id))
        )
        return update_goal(*args, **kwargs)

    monkeypatch.setattr(comms.goals, "update_goal", edit_before_cas)
    with pytest.raises(RuntimeError, match="changed"):
        await proxy.request(
            "update_goal", status="paused", goal_id=goal.id, expected_revision=goal.revision
        )
    assert comms.registry.require(session).goal == changed
    assert changed.text == "New objective" and changed.state.declared_name == "active"
    assert scheduled == []
