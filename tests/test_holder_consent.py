"""Shared maintenance, exact holder consent and genuine caller binding are different contracts."""

from dataclasses import replace
import time

import pytest

from agent_comms.errors import TodoConflict
from agent_comms.goal_actions import OrderGoalAction, ReissuedOrderGoalAction
from agent_comms.nominal.field_codec import GoalId, OrderId
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_identity import CoreWatcherIncarnation, RecordedThreadIncarnation
from agent_comms.todos import AssignTodo, HolderConsentRequired, ReleaseTodo, StrikeReason
from agent_comms.tool_caller import CommanderForkCaller, CommanderPeerCaller, ThreadCaller

from test_commander_forks import fork_bus  # noqa: F401

RELEASE = StrikeReason("The current holder explicitly releases its exact recorded work assignment.")


@pytest.mark.parametrize("name", ("lead", "other", "worker"))
def test_shared_maintenance_and_storage_selection_do_not_release_another_holder(fork_bus, name):
    comms, _, _ = fork_bus
    store = comms.goals.todos
    current = store.get("P40")
    caller = CommanderPeerCaller(name).as_thread("retained")
    actor = caller.identity(comms)
    assert actor.commands(current.creator_identity)
    assert actor.commanding_identity == comms.registry.require(name).incarnation
    assert not actor.represents(current.held().owner_identity)
    with pytest.raises(HolderConsentRequired):
        ReleaseTodo(current.revision, actor, current.held(), RELEASE).record(comms.goals, current.id)
    assert store.get(current.id) == current


@pytest.mark.parametrize("caller", (ThreadCaller("retained"), CommanderForkCaller("holder-child")))
def test_holder_and_its_bound_in_turn_child_can_release_the_exact_hold(fork_bus, caller):
    comms, _, _ = fork_bus
    current = comms.goals.todos.get("P40")
    caller.join(comms, "retained")
    actor = caller.identity(comms)
    assert actor.represents(current.held().owner_identity)
    released = ReleaseTodo(current.revision, actor, current.held(), RELEASE).record(
        comms.goals, current.id)
    assert released.assignment is None
    event, = released.history.events(released.id, ReleaseTodo)
    assert event.actor == actor and event.previous == current.held()


def test_raw_child_identity_does_not_inherit_a_parents_holder_consent(fork_bus):
    comms, _, _ = fork_bus
    child = CommanderForkCaller("holder-child")
    child.join(comms, "retained")
    current = comms.goals.todos.get("P40")
    actor = ThreadCaller(child.name).identity(comms)
    assert not actor.represents(current.held().owner_identity)
    with pytest.raises(HolderConsentRequired):
        ReleaseTodo(current.revision, actor, current.held(), RELEASE).record(comms.goals, current.id)
    assert comms.goals.todos.get(current.id) == current


def test_reused_holder_name_has_no_consent_from_its_previous_birth(fork_bus):
    comms, _, _ = fork_bus
    current = comms.goals.todos.get("P40")
    previous = current.held().owner_identity
    actor = RecordedThreadIncarnation(previous.name, previous.wire_birth + 1).as_colleague()
    with pytest.raises(HolderConsentRequired):
        ReleaseTodo(current.revision, actor, current.held(), RELEASE).record(comms.goals, current.id)
    assert comms.goals.todos.get(current.id) == current


def test_consent_cannot_release_a_different_assignment_generation(fork_bus):
    comms, _, _ = fork_bus
    current = comms.goals.todos.get("P40")
    actor = comms.registry.require("retained").acting_identity
    wrong = replace(current.held(), generation=GoalId.mint())
    with pytest.raises(TodoConflict):
        ReleaseTodo(current.revision, actor, wrong, RELEASE).record(comms.goals, current.id)
    assert comms.goals.todos.get(current.id) == current


def handoff(comms, current, actor):
    return comms.goals.update_goal("worker", OrderGoalAction(
        problem=current.id, expected_revision=current.revision,
        assigner=actor, commander="lead", request=current.request.require(),
        review_at=RecordedEventTime(time.time() + 600),
        order=replace(current.order, id=OrderId.mint())))


def test_maintainer_cannot_change_holder_or_the_receiving_goal(fork_bus):
    comms, _, _ = fork_bus
    current = comms.goals.todos.get("P40")
    goal = comms.registry.require("worker").goal
    actor = CommanderPeerCaller("lead").as_thread("retained").identity(comms)
    with pytest.raises(HolderConsentRequired):
        handoff(comms, current, actor)
    assert comms.goals.todos.get(current.id) == current
    assert comms.registry.require("worker").goal == goal


def test_original_issuer_cannot_replace_the_order_while_keeping_its_assignment(fork_bus):
    comms, _, _ = fork_bus
    current = comms.goals.todos.get("P40")
    actor = comms.registry.require("lead").acting_identity
    operation = AssignTodo.by(
        actor, current.revision, current.held().owner_identity, current.held().generation,
        current.review_at, replace(current.order, id=OrderId.mint()),
        current.request.require(), current.order.owner, frozenset({current.id}))
    assert operation.proposed == current.assignment
    with pytest.raises(HolderConsentRequired):
        operation.record(comms.goals, current.id)
    assert comms.goals.todos.get(current.id) == current


def test_holder_handoff_keeps_creator_history_and_records_the_actual_actor(fork_bus):
    comms, _, _ = fork_bus
    current = comms.goals.todos.get("P40")
    actor = comms.registry.require("retained").acting_identity
    handoff(comms, current, actor)
    changed = comms.goals.todos.get(current.id)
    worker = comms.registry.require("worker")
    assert changed.creator_identity == current.creator_identity
    assert changed.held().owner_identity == worker.incarnation
    assert changed.held().parent_identity == actor
    assert changed.held().generation == worker.goal.require().id
    assert changed.held().previous_owners == (current.held().owner_identity,)


def test_reissue_records_core_instead_of_reconstructing_the_original_assigner(fork_bus):
    comms, _, _ = fork_bus
    current = comms.goals.todos.get("P40")
    actor = CoreWatcherIncarnation.watcher()
    action = ReissuedOrderGoalAction.for_todo(current, "lead")
    assert action.assigner == actor
    assert actor.commands(current.creator_identity)
    assert not actor.represents(current.held().owner_identity)
    assert actor.answerable((current.creator_identity,)) == ()
