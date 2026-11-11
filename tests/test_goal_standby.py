"""Declared goal dependencies control scheduling."""

import os
from dataclasses import replace

import pytest

from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.nominal.field_codec import GoalWaitId, TurnId
from agent_comms.planning.goal_actions import (
    EditGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.planning.goal_presentation import RunnableExecution, StandbyExecution
from agent_comms.planning.goal_scope import OneGoal
from agent_comms.planning.goal_wait_target import GoalWaitTarget
from agent_comms.planning.goal_waits import GoalWaits, NoGoalWait, RecordedGoalWait
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_status import PausedBridgeThreadStatus
from agent_comms.registry.threads import NativeThread
from goal_owner_fixture import canonical_goal_wire


def live_wire(tmp_path, *names, working=()):
    """A canonical goal wire whose named threads run in this process."""
    comms = canonical_goal_wire(tmp_path)
    for name in names:
        comms.registry.declare(NativeThread(
            name, frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    for name in working:
        comms.agents.begin_turn(name, TurnId.mint())
    return comms


def standby(comms, name, goal, *targets):
    return comms.goals.update_goal(name, StandbyGoalAction(
        expect=GoalPrecondition(goal_id=OneGoal(goal.id)), wait_for=targets))


def unchecked_cycle(comms, tmp_path, *, bound_turn):
    """Alice waits on Bob, plus Bob's wait on Alice recorded before cycles were refused."""
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    standby(comms, "alice", alice, "bob")
    owner, peer = comms.registry.require("bob"), comms.registry.require("alice")
    GoalWaits(tmp_path).record(RecordedGoalWait(
        bob.id, GoalWaitId.mint(), bob.revision, 0,
        (GoalWaitTarget(peer.incarnation.require_recorded()),),
        owner=owner.incarnation.require_recorded(),
        target_turn_generations=(peer.turn_generation,),
        report_turn_id=owner.turn_lease.turn_id if bound_turn else None,
        report_turn_generation=owner.turn_generation if bound_turn else None,
    ))


def test_edit_preserves_owner_pause_and_standby_requires_declared_targets(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    comms.registry.declare(NativeThread('parent', frozenset(), Checkout(tmp_path)))
    goal = comms.goals.update_goal("parent", SetGoalAction(text="Goal with @mention"))
    with pytest.raises(ValueError):
        standby(comms, "parent", goal)
    comms.goals.update_goal("parent", PausedGoalAction(), actor=OwnerInvocable)
    comms.goals.update_goal("parent", EditGoalAction(text="Edited @mention"))
    assert comms.registry.require("parent").goal.state.pause_source.declared_name == "owner"


def test_standby_rejects_closed_wait_cycle_while_both_turns_are_active(tmp_path):
    comms = live_wire(tmp_path, "alice", "bob", working=("alice", "bob"))
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    standby(comms, "alice", alice, "bob")
    alice_wait = comms.goals.goal_wait("alice")
    assert alice_wait.report_turn_id == comms.registry.require("alice").turn_lease.turn_id
    assert alice_wait.report_turn_generation == comms.registry.require("alice").turn_generation
    with pytest.raises(ValueError):
        standby(comms, "bob", bob, "alice")
    assert isinstance(comms.goals.goal_wait("alice"), RecordedGoalWait)
    assert isinstance(comms.goals.goal_wait("bob"), NoGoalWait)
    assert comms.registry.require("bob").goal == bob


@pytest.mark.parametrize("carol", ["working", "idle"])
def test_live_alternative_keeps_wait_cycle_open(tmp_path, carol):
    working = ("alice", "bob", "carol") if carol == "working" else ("alice", "bob")
    comms = live_wire(tmp_path, "alice", "bob", "carol", working=working)
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob or Carol"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    comms.goals.update_goal("carol", SetGoalAction(text="Carol's own goal"))
    standby(comms, "alice", alice, "bob", "carol")
    standby(comms, "bob", bob, "alice")
    assert isinstance(comms.goals.goal_wait("alice"), RecordedGoalWait)
    assert isinstance(comms.goals.goal_wait("bob"), RecordedGoalWait)
    assert isinstance(comms.goals.goal_execution("bob"), StandbyExecution)


def test_dead_active_turn_does_not_make_wait_cycle_runnable(tmp_path):
    comms = live_wire(tmp_path, "alice", "bob", "carol", working=("alice", "bob", "carol"))
    carol_thread = comms.registry.require("carol")
    assert carol_thread.executing
    record = carol_thread.require_turn_lease().record
    record.begin(record.registry_path, record.identity,
                 replace(record.read().require_active(), owner=ProcessIdentity(999999999, 1)),
                 registry_type=record.registry_type)
    comms.registry.register(
        replace(carol_thread, process_identity=ProcessIdentity(999999999, 1)),
        comms.registry.status("carol"),
    )
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob or Carol"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    standby(comms, "alice", alice, "bob", "carol")
    with pytest.raises(ValueError):
        standby(comms, "bob", bob, "alice")


def test_standby_entry_accepts_idle_live_dependency(tmp_path):
    comms = live_wire(tmp_path, "alice", "bob")
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    assert not comms.agents.is_working(
        comms.registry.require("bob").incarnation, comms.registry.snapshot())
    standby(comms, "alice", alice, "bob")
    assert isinstance(comms.goals.goal_wait("alice"), RecordedGoalWait)
    assert comms.goals.recover_closed_goal_wait("alice") == ()
    assert isinstance(comms.goals.goal_execution("alice"), StandbyExecution)


def test_idle_live_dependency_keeps_wait_group_open(tmp_path):
    comms = live_wire(tmp_path, "alice", "bob", working=("alice", "bob"))
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    comms.goals.update_goal("bob", SetGoalAction(text="Finish work and answer later"))
    standby(comms, "alice", alice, "bob")
    comms.agents.finish_turn(comms.registry.require("alice").turn_lease)
    comms.agents.finish_turn(comms.registry.require("bob").turn_lease)

    assert isinstance(comms.goals.goal_wait("bob"), NoGoalWait)
    assert not comms.agents.is_working(
        comms.registry.require("bob").incarnation, comms.registry.snapshot())
    assert comms.goals.recover_closed_goal_wait("alice") == ()
    assert isinstance(comms.goals.goal_wait("alice"), RecordedGoalWait)
    assert isinstance(comms.goals.goal_execution("alice"), StandbyExecution)


@pytest.mark.parametrize("pending_reply", [False, True])
def test_liveness_check_releases_preexisting_closed_wait_group(tmp_path, pending_reply):
    comms = live_wire(tmp_path, "alice", "bob", working=("alice", "bob"))
    unchecked_cycle(comms, tmp_path, bound_turn=True)
    comms.agents.finish_turn(comms.registry.require("alice").turn_lease)
    comms.agents.finish_turn(comms.registry.require("bob").turn_lease)

    if pending_reply:
        comms.messaging.send("bob", "alice", "The work is finished").message
        assert comms.goals.recover_closed_goal_wait("alice") == ()
        assert isinstance(comms.goals.goal_wait("alice"), RecordedGoalWait)
        return

    assert comms.goals.recover_closed_goal_wait("alice") == ("alice", "bob")
    assert isinstance(comms.goals.goal_wait("alice"), NoGoalWait)
    assert isinstance(comms.goals.goal_execution("alice"), RunnableExecution)
    assert "Standby was released" in comms.registry.require("alice").goal.progress
    assert comms.goals.recover_closed_goal_wait("alice") == ()


@pytest.mark.parametrize("bound_old_turn", [False, True])
def test_new_live_dependency_turn_keeps_old_wait_group_open(tmp_path, bound_old_turn):
    comms = live_wire(tmp_path, "alice", "bob", working=("alice", "bob"))
    unchecked_cycle(comms, tmp_path, bound_turn=bound_old_turn)
    comms.agents.finish_turn(comms.registry.require("alice").turn_lease)
    comms.agents.finish_turn(comms.registry.require("bob").turn_lease)
    comms.agents.begin_turn("bob", TurnId('51baa1dde83052cee4eb9bef5397a63c'))
    wait = comms.goals.goal_wait("alice")
    assert wait is not None
    assert comms.goals.recover_closed_goal_wait("alice") == ()
    assert comms.goals.goal_wait("alice") == wait
    assert isinstance(comms.goals.goal_execution("alice"), StandbyExecution)
    comms.agents.finish_turn(comms.registry.require("bob").turn_lease)
    assert comms.goals.recover_closed_goal_wait("alice") == ("alice", "bob")


@pytest.mark.parametrize("read_by_human", [False, True])
def test_standby_refuses_canonical_reply_that_arrived_before_wait(tmp_path, read_by_human):
    comms = canonical_goal_wire(tmp_path)
    for name in ("parent", "child"):
        comms.registry.declare(NativeThread(name, frozenset(), Checkout(tmp_path)))
    goal = comms.goals.update_goal("parent", SetGoalAction(text="Delegate work"))
    comms.messaging.send("child", "parent", "Finished immediately")
    if read_by_human:
        comms.bus.mark_read_through("parent", comms.bus.inbox("parent").page.messages[-1].seq)
    with pytest.raises(ValueError):
        standby(comms, "parent", goal, "child")
    assert isinstance(comms.goals.goal_wait("parent"), NoGoalWait)
    assert comms.registry.require("parent").goal == goal
    assert not (tmp_path / "input_dispositions.sqlite3").exists()


@pytest.mark.parametrize("bridge_alive", [True, False])
def test_standby_on_a_paused_bridge_waits_only_while_its_process_lives(tmp_path, bridge_alive):
    comms = live_wire(tmp_path, "waiter")
    teammate = NativeThread("teammate", frozenset(), Checkout(tmp_path))
    if bridge_alive:
        teammate = replace(teammate, process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.register(teammate, PausedBridgeThreadStatus())
    goal = comms.goals.update_goal("waiter", SetGoalAction(text="Wait for the teammate's verdict"))
    if bridge_alive:
        standby(comms, "waiter", goal, "teammate")
        assert comms.goals.recover_closed_goal_wait("waiter") == ()
        assert isinstance(comms.goals.goal_execution("waiter"), StandbyExecution)
    else:
        with pytest.raises(ValueError, match="will answer"):
            standby(comms, "waiter", goal, "teammate")
