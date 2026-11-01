"""Shared maintenance, exact holder consent and genuine caller binding are different contracts."""

from contextlib import nullcontext
from dataclasses import replace
import time

import pytest

from agent_comms.errors import RelationViolationError, TodoConflict
from agent_comms.goal_actions import CreateProblemGoalAction, OrderGoalAction, OwnerInvocable, PausedGoalAction, ReissuedOrderGoalAction, SetGoalAction
from agent_comms.goal_states import ActiveGoal, BlockedGoal, CompletedGoal, ModelPause, PausedGoal
from agent_comms.goals import Goal, GoalConsentRequired, NoGoal
from agent_comms.order_tools import CommsChangeProblemTool, CommsOrderTool, CommsTodoCreateTool
from agent_comms.nominal.field_codec import GoalId, OrderId
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_identity import CoreWatcherIncarnation, RecordedThreadIncarnation
from agent_comms.threads import ParkedThreadWorkState
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


def incoming_work(comms):
    current = comms.goals.todos.get("P40")
    actor = comms.registry.require("retained").acting_identity
    released = ReleaseTodo(current.revision, actor, current.held(), RELEASE).record(
        comms.goals, current.id)
    action = OrderGoalAction(
        problem=released.id, expected_revision=released.revision,
        assigner=comms.registry.require("lead").acting_identity, commander="lead",
        request=released.request.require(), review_at=RecordedEventTime(time.time() + 600),
        order=replace(released.order, id=OrderId.mint()))
    return released, action


@pytest.mark.parametrize("caller", (
    ThreadCaller("lead"), ThreadCaller("other"),
    CommanderPeerCaller("lead").as_thread("worker"),
))
def test_fresh_goal_observation_and_shared_maintenance_are_not_receiving_consent(fork_bus, caller):
    comms, _, _ = fork_bus
    current, action = incoming_work(comms)
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Keep my independent work."))
    action = replace(action, expect=replace(action.expect, expected_checkpoint=goal.captured))
    generation = comms.goals.open_goal_store().require_generation(goal.id)
    with pytest.raises(GoalConsentRequired):
        caller.invoke(CommsOrderTool(owner="worker", order=action), comms)
    assert comms.registry.require("worker").goal == goal
    assert comms.goals.todos.get(current.id) == current
    assert comms.goals.open_goal_store().require_generation(goal.id) == generation


def test_low_level_todo_tool_cannot_attach_work_behind_an_independent_goal(fork_bus):
    comms, _, _ = fork_bus
    current, action = incoming_work(comms)
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Keep my independent work."))
    worker = comms.registry.require("worker")
    operation = AssignTodo.by(
        action.assigner, current.revision, worker.incarnation, goal.id,
        action.review_at, action.order, action.request, action.traced, frozenset({current.id}))
    with pytest.raises(GoalConsentRequired):
        ThreadCaller("lead").invoke(CommsChangeProblemTool(
            problem=current.id, operation=operation), comms)
    assert comms.registry.require("worker").goal == goal
    assert comms.goals.todos.get(current.id) == current


def test_creating_a_problem_does_not_replace_an_independent_goal(fork_bus):
    comms, source, _ = fork_bus
    current, action = incoming_work(comms)
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Keep my independent work."))
    creation = CreateProblemGoalAction(
        problem="P41", repo=source.github, text="Another declared repair",
        assigner=action.assigner, commander=action.commander, request=action.request,
        review_at=action.review_at, order=action.order)
    rows = comms.goals.todos.board()
    with pytest.raises(GoalConsentRequired):
        ThreadCaller("lead").invoke(CommsTodoCreateTool(owner="worker", order=creation), comms)
    assert comms.goals.todos.board() == rows
    assert comms.registry.require("worker").goal == goal
    assert comms.goals.todos.get(current.id) == current


@pytest.mark.parametrize("bound_child", (False, True))
def test_receiving_owner_or_genuine_child_can_accept_work_into_its_goal(fork_bus, bound_child):
    comms, _, _ = fork_bus
    current, action = incoming_work(comms)
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Choose the next repair."))
    caller = CommanderPeerCaller("worker").as_thread("lead")
    if bound_child:
        caller = CommanderForkCaller("goal-child")
        caller.join(comms, "worker")
    caller.invoke(CommsOrderTool(owner="worker", order=action), comms)
    worker = comms.registry.require("worker")
    assigned = comms.goals.todos.get(current.id)
    assert worker.goal.require().id == goal.id
    assert worker.goal.require().revision == goal.revision + 1
    assert assigned.held().owned_by(worker.incarnation, goal.id)
    assert assigned.held().parent_identity == caller.identity(comms)


def test_core_reissues_the_exact_accepted_unheld_order_into_a_free_goal_slot(fork_bus):
    comms, _, _ = fork_bus
    current, _ = incoming_work(comms)
    action = ReissuedOrderGoalAction.for_todo(current, "lead")
    goal = comms.goals.update_goal("worker", action)
    assigned = comms.goals.todos.get(current.id)
    assert assigned.order == action.order
    assert assigned.creator_identity == current.creator_identity
    assert assigned.held().owned_by(comms.registry.require("worker").incarnation, goal.id)
    assert assigned.held().parent_identity == CoreWatcherIncarnation.watcher()


def test_core_reissue_cannot_override_an_existing_independent_goal(fork_bus):
    comms, _, _ = fork_bus
    current, _ = incoming_work(comms)
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Keep my independent work."))
    with pytest.raises(GoalConsentRequired):
        comms.goals.update_goal("worker", ReissuedOrderGoalAction.for_todo(current, "lead"))
    assert comms.registry.require("worker").goal == goal
    assert comms.goals.todos.get(current.id) == current


def test_raw_child_does_not_supply_receiving_goal_consent(fork_bus):
    comms, _, _ = fork_bus
    current, action = incoming_work(comms)
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Keep my independent work."))
    CommanderForkCaller("goal-child").join(comms, "worker")
    with pytest.raises(GoalConsentRequired):
        ThreadCaller("goal-child").invoke(CommsOrderTool(owner="worker", order=action), comms)
    assert comms.registry.require("worker").goal == goal
    assert comms.goals.todos.get(current.id) == current


def test_receiving_consent_does_not_resume_an_owner_pause(fork_bus):
    comms, _, _ = fork_bus
    current, action = incoming_work(comms)
    comms.goals.update_goal("worker", SetGoalAction(text="Keep my independent work."))
    goal = comms.goals.update_goal(
        "worker", PausedGoalAction(), actor=OwnerInvocable)
    with pytest.raises(ValueError) as refused:
        CommanderPeerCaller("worker").as_thread("lead").invoke(
            CommsOrderTool(owner="worker", order=action), comms)
    assert str(refused.value) == goal.state.pause_source.instruction()
    assert comms.registry.require("worker").goal == goal
    assert comms.goals.todos.get(current.id) == current


def test_stale_receiver_record_cannot_bypass_creation_consent(fork_bus):
    comms, source, _ = fork_bus
    current, action = incoming_work(comms)
    stale = comms.registry.require("worker")
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Keep my independent work."))
    rows = comms.goals.todos.board()
    with pytest.raises(GoalConsentRequired):
        comms.goals.todos.create(
            "P41", source.github, "Another declared repair",
            creator=comms.registry.require("lead"), owner=stale, assigner=action.assigner,
            generation=GoalId.mint(), request=action.request,
            review_at=action.review_at, order=action.order)
    assert comms.goals.todos.board() == rows
    assert comms.registry.require("worker").goal == goal
    assert comms.goals.todos.get(current.id) == current


def test_exact_noop_maintenance_needs_no_new_goal_consent(fork_bus):
    comms, _, _ = fork_bus
    current, action = incoming_work(comms)
    ThreadCaller("lead").invoke(CommsOrderTool(owner="worker", order=action), comms)
    current = comms.goals.todos.get(current.id)
    worker = comms.registry.require("worker")
    operation = AssignTodo.by(
        action.assigner, current.revision, worker.incarnation, worker.goal.require().id,
        current.review_at, current.order.checked(), current.request.require(),
        action.traced, frozenset({current.id}))
    result = ThreadCaller("lead").invoke(
        CommsChangeProblemTool(problem=current.id, operation=operation), comms)
    assert result.todo == current
    assert comms.registry.require("worker").goal == worker.goal


@pytest.mark.parametrize("parked", (False, True))
def test_post_handoff_review_timer_update_preserves_history_without_new_consent(fork_bus, parked):
    comms, _, _ = fork_bus
    current, action = incoming_work(comms)
    ThreadCaller("lead").invoke(CommsOrderTool(owner="worker", order=action), comms)
    current = comms.goals.todos.get(current.id)
    worker = comms.registry.require("worker")
    assert current.held().previous_owners
    if parked:
        comms.registry.set_work_state("worker", ParkedThreadWorkState())
    review_at = RecordedEventTime(current.review_at.recorded_at("A review time") + 1)
    operation = AssignTodo.by(
        action.assigner, current.revision, worker.incarnation, worker.goal.require().id,
        review_at, current.order.checked(), current.request.require(),
        action.traced, frozenset({current.id}))
    assert operation.proposed.previous_owners == ()
    result = ThreadCaller("lead").invoke(
        CommsChangeProblemTool(problem=current.id, operation=operation), comms)
    assert result.todo.review_at == review_at
    assert result.todo.revision == current.revision + 1
    assert result.todo.assignment == current.assignment
    assert result.todo.order == current.order
    assert result.todo.creator_identity == current.creator_identity
    assert comms.registry.require("worker").goal == worker.goal


@pytest.fixture(params=(False, True), ids=("absent-goal", "completed-goal"))
def free_receiver(fork_bus, request):
    comms, _, _ = fork_bus
    worker = comms.registry.require("worker")
    if request.param:
        goal = Goal(text="A completed independent task", id=GoalId.mint(),
                    published_seq=0, state=CompletedGoal())
        comms.registry.register(replace(worker, goal=goal))
    return comms.registry.require("worker")


@pytest.mark.parametrize("parked", (False, True))
def test_direct_assignment_consumes_the_fresh_receivers_work_state(fork_bus, free_receiver, parked):
    comms, source, _ = fork_bus
    current, action = incoming_work(comms)
    if parked:
        comms.registry.set_work_state("worker", ParkedThreadWorkState())
    rows = comms.goals.todos.board()
    worktrees = source.git(source.checkout, "worktree", "list", "--porcelain")
    operation = AssignTodo.by(
        action.assigner, current.revision, free_receiver.incarnation, GoalId.mint(),
        action.review_at, action.order, action.request, action.traced, frozenset({current.id}))
    with pytest.raises(RelationViolationError) if parked else nullcontext():
        ThreadCaller("lead").invoke(
            CommsChangeProblemTool(problem=current.id, operation=operation), comms)
    assert comms.registry.require("worker").goal == free_receiver.goal
    if parked:
        assert comms.goals.todos.board() == rows
        assert source.git(source.checkout, "worktree", "list", "--porcelain") == worktrees
    else:
        assert comms.goals.todos.get(current.id).held().owner_identity == free_receiver.incarnation


@pytest.mark.parametrize("parked", (False, True))
def test_creation_rechecks_work_state_instead_of_trusting_a_retained_ready_thread(
    fork_bus, free_receiver, parked,
):
    comms, source, _ = fork_bus
    _, action = incoming_work(comms)
    if parked:
        comms.registry.set_work_state("worker", ParkedThreadWorkState())
    rows = comms.goals.todos.board()
    worktrees = source.git(source.checkout, "worktree", "list", "--porcelain")
    with pytest.raises(RelationViolationError) if parked else nullcontext():
        comms.goals.todos.create(
            "P41", source.github, "Another declared repair",
            creator=comms.registry.require("lead"), owner=free_receiver,
            assigner=action.assigner, generation=GoalId.mint(), request=action.request,
            review_at=action.review_at, order=action.order)
    assert comms.registry.require("worker").goal == free_receiver.goal
    if parked:
        assert comms.goals.todos.board() == rows
        assert source.git(source.checkout, "worktree", "list", "--porcelain") == worktrees
    else:
        assert comms.goals.todos.get("P41").held().owner_identity == free_receiver.incarnation


@pytest.mark.parametrize("state,available", (
    (ActiveGoal(), False), (BlockedGoal("Needs an explicit retry."), False),
    (PausedGoal(), False), (PausedGoal(ModelPause()), False), (CompletedGoal(), True),
))
def test_automatic_dispatch_uses_goal_owned_availability(fork_bus, state, available):
    comms, _, _ = fork_bus
    thread = comms.registry.require("worker")
    goal = Goal(text="Independent work", id=GoalId.mint(), published_seq=0, state=state)
    thread = replace(thread, goal=goal)
    assert thread.execution.order_candidates(thread) == ((thread,) if available else ())
    actor = comms.registry.require("lead").acting_identity
    if available:
        thread.goal.require_work(actor, thread)
    else:
        with pytest.raises(ValueError):
            thread.goal.require_work(actor, thread)
    free = replace(thread, goal=NoGoal())
    assert free.execution.order_candidates(free) == (free,)
