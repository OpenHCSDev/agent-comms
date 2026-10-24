"""Declared goal dependencies control scheduling without changing legacy registry rows."""

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import GoalWaitId, TurnId
from agent_comms.goal_actions import (
    EditGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.goal_presentation import RunnableExecution, StandbyExecution, GoalWaitTarget
from agent_comms.goal_waits import GoalWaits
from goal_owner_fixture import canonical_goal_wire
from agent_comms.goal_waits import RecordedGoalWait, NoGoalWait
from agent_comms.threads import Thread


def test_edit_preserves_owner_pause_and_standby_requires_declared_targets(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    comms.registry.declare(Thread('parent', frozenset(), str(tmp_path)))
    goal = comms.goals.update_goal("parent", SetGoalAction(text="Goal with @mention"))
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "parent", StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id))
        )
    comms.goals.update_goal("parent", PausedGoalAction(), actor=OwnerInvocable)
    comms.goals.update_goal("parent", EditGoalAction(text="Edited @mention"))
    assert comms.registry.require("parent").goal.state.pause_source.declared_name == "owner"


def test_standby_rejects_closed_wait_cycle_while_both_turns_are_active(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    for name in ("alice", "bob"):
        comms.registry.declare(
            Thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
        comms.agents.begin_turn(name, TurnId.mint())
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    assert alice is not None and bob is not None

    comms.goals.update_goal(
        "alice", StandbyGoalAction(expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob",))
    )
    alice_wait = comms.goals.goal_wait("alice")
    assert alice_wait is not None
    assert alice_wait.report_turn_id == comms.registry.require("alice").turn_lease.turn_id
    assert alice_wait.report_turn_generation == comms.registry.require("alice").turn_generation
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "bob", StandbyGoalAction(expect=GoalPrecondition(goal_id=bob.id), wait_for=("alice",))
        )

    assert isinstance(comms.goals.goal_wait("alice"), RecordedGoalWait)
    assert isinstance(comms.goals.goal_wait("bob"), NoGoalWait)
    assert comms.registry.require("bob").goal == bob


def test_standby_allows_independent_alternative_to_wait_cycle(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    for name in ("alice", "bob", "carol"):
        comms.registry.declare(
            Thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
        comms.agents.begin_turn(name, TurnId.mint())
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

    assert isinstance(comms.goals.goal_wait("alice"), RecordedGoalWait)
    assert isinstance(comms.goals.goal_wait("bob"), RecordedGoalWait)


def test_idle_live_alternative_keeps_wait_cycle_open(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    for name in ("alice", "bob", "carol"):
        comms.registry.declare(
            Thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
    for name in ("alice", "bob"):
        comms.agents.begin_turn(name, TurnId.mint())
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob or Carol"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    carol = comms.goals.update_goal("carol", SetGoalAction(text="Idle goal"))
    assert alice is not None and bob is not None and carol is not None

    comms.goals.update_goal(
        "alice",
        StandbyGoalAction(expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob", "carol")),
    )
    comms.goals.update_goal(
        "bob", StandbyGoalAction(expect=GoalPrecondition(goal_id=bob.id), wait_for=("alice",))
    )

    assert isinstance(comms.goals.goal_wait("bob"), RecordedGoalWait)
    assert isinstance(comms.goals.goal_execution("bob"), StandbyExecution)


def test_dead_active_turn_does_not_make_wait_cycle_runnable(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    for name in ("alice", "bob"):
        comms.registry.declare(
            Thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
        comms.agents.begin_turn(name, TurnId.mint())
    comms.registry.declare(
        Thread('carol', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    comms.agents.begin_turn("carol", TurnId('91a3ae9c092f86bc92b86f06b3eb613a'))
    carol_thread = comms.registry.require("carol")
    assert carol_thread.executing
    record = carol_thread.require_turn_lease().record
    record.begin(record.registry_path, record.identity,
                 replace(record.read().require_active(), owner=ProcessIdentity(999999999, 1)))
    comms.registry.register(
        replace(
            carol_thread,
            process_identity=ProcessIdentity(999999999, 1),
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
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "bob", StandbyGoalAction(expect=GoalPrecondition(goal_id=bob.id), wait_for=("alice",))
        )


def test_standby_entry_accepts_idle_live_dependency(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    for name in ("alice", "bob"):
        comms.registry.declare(Thread(
            name, frozenset(), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid())))
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    assert not comms.agents.is_working(
        comms.registry.require("bob").incarnation, comms.registry.snapshot())
    comms.goals.update_goal("alice", StandbyGoalAction(
        expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob",)))
    assert isinstance(comms.goals.goal_wait("alice"), RecordedGoalWait)
    assert comms.goals.recover_closed_goal_wait("alice") == ()
    assert isinstance(comms.goals.goal_execution("alice"), StandbyExecution)


def test_idle_live_dependency_keeps_wait_group_open(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    for name in ("alice", "bob"):
        comms.registry.declare(Thread(
            name, frozenset(), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid())))
        comms.agents.begin_turn(name, TurnId.mint())
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    comms.goals.update_goal("bob", SetGoalAction(text="Finish work and answer later"))
    comms.goals.update_goal("alice", StandbyGoalAction(
        expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob",)))
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
    comms = canonical_goal_wire(tmp_path)
    for name in ("alice", "bob"):
        comms.registry.declare(
            Thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
        comms.agents.begin_turn(name, TurnId.mint())
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    assert alice is not None and bob is not None
    comms.goals.update_goal(
        "alice", StandbyGoalAction(expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob",))
    )

    # Represent a wait recorded by an older owner before cycle claim_scope was
    # enforced. Both turns subsequently finish without any dependency reply.
    owner = comms.registry.require("bob")
    peer = comms.registry.require("alice")
    GoalWaits(tmp_path / "goal_waits.sqlite3").record(
        RecordedGoalWait(
            bob.id,
            GoalWaitId.mint(),
            bob.revision,
            0,
            (GoalWaitTarget(peer.incarnation.require_recorded()),),
            owner=owner.incarnation.require_recorded(),
            target_turn_generations=(peer.turn_generation,),
            report_turn_id=owner.turn_record.read().require_active().id,
            report_turn_generation=owner.turn_generation,
        )
    )
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
    comms = canonical_goal_wire(tmp_path)
    for name in ("alice", "bob"):
        comms.registry.declare(
            Thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
        comms.agents.begin_turn(name, TurnId.mint())
    alice = comms.goals.update_goal("alice", SetGoalAction(text="Wait for Bob"))
    bob = comms.goals.update_goal("bob", SetGoalAction(text="Wait for Alice"))
    assert alice is not None and bob is not None
    comms.goals.update_goal(
        "alice", StandbyGoalAction(expect=GoalPrecondition(goal_id=alice.id), wait_for=("bob",))
    )
    owner = comms.registry.require("bob")
    peer = comms.registry.require("alice")
    GoalWaits(tmp_path / "goal_waits.sqlite3").record(
        RecordedGoalWait(
            bob.id,
            GoalWaitId.mint(),
            bob.revision,
            0,
            (GoalWaitTarget(peer.incarnation.require_recorded()),),
            owner=owner.incarnation.require_recorded(),
            target_turn_generations=(peer.turn_generation,),
            report_turn_id=owner.turn_lease.turn_id if bound_old_turn else None,
            report_turn_generation=owner.turn_generation if bound_old_turn else None,
        )
    )
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


@pytest.mark.parametrize("acknowledged", [False, True])
def test_standby_refuses_canonical_reply_that_arrived_before_wait(tmp_path, acknowledged):
    comms = canonical_goal_wire(tmp_path)
    for name in ("parent", "child"):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    goal = comms.goals.update_goal("parent", SetGoalAction(text="Delegate work"))
    message = comms.messaging.send("child", "parent", "Finished immediately").message
    if acknowledged:
        references = tuple(message.reference for message in comms.bus.inbox("parent").page.messages)
        comms.messaging.acknowledge("parent", references)
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "parent",
            StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("child",)),
        )
    assert isinstance(comms.goals.goal_wait("parent"), NoGoalWait)
    assert comms.registry.require("parent").goal == goal
    assert not (tmp_path / "input_dispositions.sqlite3").exists()
