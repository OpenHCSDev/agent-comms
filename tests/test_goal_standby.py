"""Declared goal dependencies control scheduling without changing legacy registry rows."""

import asyncio
import json
import os
from dataclasses import replace

import pytest

from agent_comms import agent_events as ae
from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    EditGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.goal_attempts import GoalAttemptStore, StaleAttemptError
from agent_comms.goal_presentation import GoalExecution, GoalExecutionState, GoalWaitTarget
from agent_comms.goal_waits import GoalWait, GoalWaits
from agent_comms.goals import Goal
from agent_comms.input_drain import InputDrain
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from agent_comms.tools import TOOLS


@pytest.mark.parametrize("wake", ["child", "owner", "revoked"])
async def test_standby_waits_for_declared_identity_and_preserves_goal_authority(
    tmp_path, monkeypatch, wake
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    monkeypatch.setenv("PI_AGENT_ID", "parent")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "parent"))
    comms.threads.register(Thread("child", frozenset(), str(tmp_path), pid=os.getpid()))
    comms.agents.begin_turn("child", "child-review-in-flight")
    comms.threads.register(Thread("other", frozenset(), str(tmp_path)))
    store = agent.turns.open_goal_store()
    goal = comms.goals.update_goal(
        "parent", SetGoalAction(text="Review @child work"), owner_store=store
    )
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
        yield ae.InputStarted(id=None)
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
            assert FieldCodec.decode(Goal, result["goal"]).state.declared_name == "active"
            assert result["goal_execution"]["state"] == "standby"
            assert [target["name"] for target in result["goal_execution"]["wait_for"]] == ["child"]
            yield ae.ToolEnd(id="wait", name="comms_goal", ok=True, output=json.dumps(result))
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="Waiting" if len(calls) == 1 else "Received")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        await agent.turns.run_agent_turn("parent", "parent", "Delegate work", autonomous_goal=True)
        current = comms.registry.require("parent").goal
        assert current.state.active and current.text == "Review @child work"
        wait = comms.goals.goal_wait("parent")
        assert wait is not None
        execution = comms.goals.goal_execution("parent")
        assert execution.state is GoalExecutionState.STANDBY
        assert (
            GoalExecution.from_wire(
                json.loads(
                    json.dumps(agent.sessions.metadata("parent")["agentComms"]["goalExecution"])
                )
            )
            == execution
        )
        assert any(
            ((row.get("_meta") or {}).get("agentComms", {}).get("goalExecution") or {}).get("state")
            == "standby"
            for row in updates
        )
        view = next(view for view in comms.views.thread_views() if view.thread.name == "parent")
        assert view.presentation.summary == "Standby · waiting for @child"
        assert store.snapshot(goal.id).number == 2
        agent.turns.schedule_goal("parent")
        assert not agent.inputs.pending_turns.get("parent")

        edited = comms.goals.update_goal(
            "parent",
            EditGoalAction(
                expect=GoalPrecondition(expected_goal=current), text="Review @child thoroughly"
            ),
        )
        assert edited.id == goal.id and edited.revision == current.revision + 1
        assert comms.goals.goal_wait("parent") == wait
        # Ordinary nondependency direct DMs now have their own no-goal-permit
        # interrupt route; the tests in test_goal_direct_interrupt cover it.
        assert store.snapshot(goal.id).number == 2

        if wake == "owner":
            await agent.inputs.run_owned_input("parent", "parent", "New owner instruction")
        else:
            comms.registry.rename("child", "renamed-child")
            message = comms.messaging.send_message(
                "renamed-child", "parent", "Implementation ready"
            )
            if wake == "revoked":
                monkeypatch.setattr(agent.inputs, "schedule_wake", lambda _session: None)
            await agent.inputs.drain_inbox("parent")
            if wake == "revoked":
                comms.goals.update_goal(
                    "parent",
                    PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
                    actor=OwnerInvocable,
                )
                InputDrain.schedule_wake(agent.inputs, "parent")
            await asyncio.wait_for(agent.inputs.wake_tasks["parent"], timeout=2)
            assert agent.inputs.dispositions.read().rows[f"bus:{message.seq}"].declared_name == (
                "unknown" if wake == "revoked" else "started"
            )
        assert len(calls) == (1 if wake == "revoked" else 2)
        assert store.snapshot(goal.id).number == (2 if wake == "revoked" else 3)
        assert comms.goals.goal_wait("parent") is None
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("changed", ["admission", "pid"])
async def test_ready_recovery_rechecks_executing_owner_before_rotating(
    tmp_path, monkeypatch, changed
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi")
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "parent"))
    store = agent.turns.open_goal_store()
    goal = comms.goals.update_goal("parent", SetGoalAction(text="Work"), owner_store=store)
    owner = comms.registry.require("parent")
    admission = comms.registry.snapshot().admission_generations["parent"]
    if changed == "admission":
        admission += 1
    else:
        owner = replace(owner, pid=owner.pid + 1)
    old_grant = store.ready_grant(goal.id, 1)
    try:
        with (
            _store_lock(comms._wire_lock_path),
            pytest.raises(StaleAttemptError, match="owner changed"),
        ):
            agent.turns.ready_goal_grant_locked(
                owner, admission, GoalAttemptStore(store.root), store.snapshot(goal.id)
            )
        assert store.ready_grant(goal.id, 1) == old_grant
    finally:
        await agent.shutdown()


def test_edit_preserves_owner_pause_and_standby_requires_declared_targets(tmp_path):
    comms = wire(tmp_path)
    comms.threads.register(Thread("parent", frozenset(), str(tmp_path)))
    goal = comms.goals.update_goal("parent", SetGoalAction(text="Goal with @mention"))
    with pytest.raises(ValueError, match="wait_for"):
        comms.goals.update_goal(
            "parent", StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id))
        )
    comms.goals.update_goal("parent", PausedGoalAction(), actor=OwnerInvocable)
    comms.goals.update_goal("parent", EditGoalAction(text="Edited @mention"))
    assert comms.goals.goal_pause("parent").source.declared_name == "owner"


def test_standby_rejects_closed_wait_cycle_while_both_turns_are_active(tmp_path):
    comms = wire(tmp_path)
    for name in ("alice", "bob"):
        comms.threads.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
        comms.agents.begin_turn(name, f"{name}-turn")
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    assert alice is not None and bob is not None

    comms.goals.update_goal(
        "alice", StandbyGoalAction(expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob",))
    )
    alice_wait = comms.goals.goal_wait("alice")
    assert alice_wait is not None
    assert alice_wait.report_turn_id == "alice-turn"
    assert alice_wait.report_turn_generation == comms.registry.require("alice").turn_generation
    with pytest.raises(ValueError, match="dependency wait group.*@alice.*@bob"):
        comms.goals.update_goal(
            "bob", StandbyGoalAction(expect=GoalPrecondition(goal_id=bob.id), wait_for=("alice",))
        )

    assert comms.goals.goal_wait("alice") is not None
    assert comms.goals.goal_wait("bob") is None
    assert comms.registry.require("bob").goal == bob


def test_standby_allows_independent_alternative_to_wait_cycle(tmp_path):
    comms = wire(tmp_path)
    for name in ("alice", "bob", "carol"):
        comms.threads.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
        comms.agents.begin_turn(name, f"{name}-turn")
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob or Carol"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    assert alice is not None and bob is not None

    comms.goals.update_goal(
        "alice",
        StandbyGoalAction(expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob", "carol")),
    )
    comms.goals.update_goal(
        "bob", StandbyGoalAction(expect=GoalPrecondition(goal_id=bob.id), wait_for=("alice",))
    )

    assert comms.goals.goal_wait("alice") is not None
    assert comms.goals.goal_wait("bob") is not None


def test_idle_active_goal_does_not_make_wait_cycle_runnable(tmp_path):
    comms = wire(tmp_path)
    for name in ("alice", "bob", "carol"):
        comms.threads.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
    for name in ("alice", "bob"):
        comms.agents.begin_turn(name, f"{name}-turn")
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob or Carol"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    carol = comms.goals.update_goal("carol", SetGoalAction(text="Idle goal"))
    assert alice is not None and bob is not None and carol is not None

    comms.goals.update_goal(
        "alice",
        StandbyGoalAction(expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob", "carol")),
    )
    with pytest.raises(ValueError, match="dependency wait group.*@alice.*@bob"):
        comms.goals.update_goal(
            "bob", StandbyGoalAction(expect=GoalPrecondition(goal_id=bob.id), wait_for=("alice",))
        )

    assert comms.goals.goal_wait("bob") is None
    assert comms.registry.require("bob").goal == bob


def test_dead_active_turn_does_not_make_wait_cycle_runnable(tmp_path):
    comms = wire(tmp_path)
    for name in ("alice", "bob"):
        comms.threads.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
        comms.agents.begin_turn(name, f"{name}-turn")
    comms.threads.register(Thread("carol", frozenset(), str(tmp_path), pid=os.getpid()))
    comms.agents.begin_turn("carol", "carol-turn")
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
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob or Carol"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    assert alice is not None and bob is not None

    comms.goals.update_goal(
        "alice",
        StandbyGoalAction(expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob", "carol")),
    )
    with pytest.raises(ValueError, match="dependency wait group.*@alice.*@bob"):
        comms.goals.update_goal(
            "bob", StandbyGoalAction(expect=GoalPrecondition(goal_id=bob.id), wait_for=("alice",))
        )


@pytest.mark.parametrize("pending_reply", [False, True])
def test_liveness_check_releases_preexisting_closed_wait_group(tmp_path, pending_reply):
    comms = wire(tmp_path)
    for name in ("alice", "bob"):
        comms.threads.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
        comms.agents.begin_turn(name, f"{name}-turn")
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    assert alice is not None and bob is not None
    comms.goals.update_goal(
        "alice", StandbyGoalAction(expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob",))
    )

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
    comms.agents.finish_turn(comms.registry.require("alice").turn_lease)
    comms.agents.finish_turn(comms.registry.require("bob").turn_lease)

    if pending_reply:
        comms.messaging.send_message("bob", "alice", "The work is finished")
        assert comms.goals.recover_closed_goal_wait("alice") == ()
        assert comms.goals.goal_wait("alice") is not None
        return

    assert comms.goals.recover_closed_goal_wait("alice") == ("alice", "bob")
    assert comms.goals.goal_wait("alice") is None
    assert comms.goals.goal_execution("alice").state is GoalExecutionState.RUNNABLE
    assert "Standby was released" in comms.registry.require("alice").goal.progress
    assert comms.goals.recover_closed_goal_wait("alice") == ()


@pytest.mark.parametrize("bound_old_turn", [False, True])
def test_new_live_dependency_turn_keeps_old_wait_group_open(tmp_path, bound_old_turn):
    comms = wire(tmp_path)
    for name in ("alice", "bob"):
        comms.threads.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
        comms.agents.begin_turn(name, f"{name}-first")
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    assert alice is not None and bob is not None
    comms.goals.update_goal(
        "alice", StandbyGoalAction(expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob",))
    )
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
            report_turn_id="bob-first" if bound_old_turn else None,
            report_turn_generation=owner.turn_generation if bound_old_turn else None,
        )
    )
    comms.agents.finish_turn(comms.registry.require("alice").turn_lease)
    comms.agents.finish_turn(comms.registry.require("bob").turn_lease)
    comms.agents.begin_turn("bob", "bob-independent-new")
    wait = comms.goals.goal_wait("alice")
    assert wait is not None
    assert comms.goals.recover_closed_goal_wait("alice") == ()
    assert comms.goals.goal_wait("alice") == wait
    assert comms.goals.goal_execution("alice").state is GoalExecutionState.STANDBY
    comms.agents.finish_turn(comms.registry.require("bob").turn_lease)
    assert comms.goals.recover_closed_goal_wait("alice") == ("alice", "bob")


def test_recheck_crash_before_wait_clear_keeps_goal_in_standby(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    for name in ("alice", "bob"):
        comms.threads.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
        comms.agents.begin_turn(name, f"{name}-turn")
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    assert alice is not None and bob is not None
    comms.goals.update_goal(
        "alice", StandbyGoalAction(expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob",))
    )
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
    comms.agents.finish_turn(comms.registry.require("alice").turn_lease)
    comms.agents.finish_turn(comms.registry.require("bob").turn_lease)
    original_clear = GoalWaits.clear

    def unavailable(*_args, **_kwargs):
        raise OSError("injected wait-clear failure")

    monkeypatch.setattr(GoalWaits, "clear", unavailable)
    with pytest.raises(OSError, match="wait-clear failure"):
        comms.goals.recover_closed_goal_wait("alice")
    reopened = wire(tmp_path)
    assert reopened.registry.require("alice").goal.state.active
    assert reopened.goals.goal_execution("alice").state is GoalExecutionState.STANDBY
    monkeypatch.setattr(GoalWaits, "clear", original_clear)
    assert reopened.goals.recover_closed_goal_wait("alice") == ("alice", "bob")
    assert reopened.goals.goal_execution("alice").state is GoalExecutionState.RUNNABLE


@pytest.mark.parametrize("already_drained", [False, True])
async def test_standby_refuses_reply_that_arrived_before_wait(
    tmp_path, monkeypatch, already_drained
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "parent"))
    comms.threads.register(Thread("child", frozenset(), str(tmp_path)))
    goal = comms.goals.update_goal("parent", SetGoalAction(text="Delegate work"))
    message = comms.messaging.send_message("child", "parent", "Finished immediately")
    try:
        if already_drained:
            await agent.inputs.drain_inbox("parent")
            assert (
                agent.inputs.dispositions.read().rows[f"bus:{message.seq}"].declared_name
                == "unknown"
            )
        with pytest.raises(ValueError, match=f"Dependency reply {message.seq}.*pending or UNKNOWN"):
            comms.goals.update_goal(
                "parent",
                StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("child",)),
            )
        assert comms.goals.goal_wait("parent") is None
        assert comms.registry.require("parent").goal == goal
        if already_drained:
            # It is still a fresh ordinary direct DM. That does not turn a
            # pre-wait reply into a qualifying declared dependency receipt.
            pending = agent.inputs.pending_turns.get("parent", [])
            assert len(pending) == 1
            assert pending[0].direct_interrupt_goal_id == goal.id
            assert pending[0].goal_wait_id is None
        else:
            assert not agent.inputs.pending_turns.get("parent")
    finally:
        await agent.shutdown()
