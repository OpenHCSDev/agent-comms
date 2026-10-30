"""Retained claim/review handoff through native Todo and goal owners on a private bus."""
from dataclasses import replace
import time

import pytest

from agent_comms.commander.order import ListedProblems
from agent_comms.errors import TodoConflict, TodoError, ToolRefusedError
from agent_comms.field_codec import GoalId, OrderId
from agent_comms.goal_actions import OrderGoalAction
from agent_comms.order_runs import ApplicationOrderRun, MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import (
    CheckedProblemOrder, CorrectRunTodo, ExpectedProblem, NotOrderCommander, OrderHeadReview,
    ReleaseTodo, StrikeReason, UnderReviewHeadVerdict, UnquotedHeadReview,
)
from test_order_claims import claimed_by_worker
from test_todos import order, thread

pytest_plugins = ('test_held_problem_orders',)

RUN = ApplicationOrderRun('The installed copied conversation retains the original answer after tab return.')
RELEASE = StrikeReason('Release the retained work to its actual successor without discarding the original claim.')


def declared_successor(ctx, source, current):
    """Issue a new declaration, never cast the retained claim/review back into pending."""
    incoming = replace(order(source), id=OrderId.mint(), run=current.order.run,
                       at=RecordedEventTime(time.time()))
    incoming = ListedProblems().carried(incoming, (current.id,), {current.id: current})
    return OrderGoalAction(problem=current.id, expected_revision=current.revision,
        order=incoming, request=current.request.require(),
        review_at=RecordedEventTime(time.time() + 600), commander=ctx.commander,
        assigner=ctx.incarnation, traced=incoming.owner)


def candidate(ctx, name='successor', birth=3.0):
    home = ctx.root / name
    home.mkdir(exist_ok=True)
    ctx.comms.registry.register(thread(name, home, birth))
    return ctx.comms.registry.require(name)


def retained_claim(ctx, source, reviewing):
    store = ctx.comms.goals.todos
    current = store.get('P1')
    CorrectRunTodo(expected_revision=current.revision, actor=ctx.incarnation,
        reason='The actual remaining installed conversation run must survive a subsequent owner change.',
        previous=current.order.run, run=RUN).record(ctx.comms.goals, current.id)
    claimed = claimed_by_worker(ctx)
    # Fixture origin is local. The declared carrier is real, and source validation never
    # talks to a network or substitutes the installed interpreter for Git evidence.
    source.git(source.checkout, 'push', 'origin',
               f'{claimed.order.claim.head}:refs/pull/{claimed.order.claim.pull_request.number}/head')
    if reviewing:
        OrderHeadReview(claimed.revision, UnquotedHeadReview.of(claimed)).record_review(
            ctx.comms.goals, claimed.order.claim.request(), UnderReviewHeadVerdict(), ctx.incarnation)
    return store.get(claimed.id)


def release(ctx, current):
    command = ReleaseTodo(current.revision, ctx.incarnation, current.held(), RELEASE)
    command.record(ctx.comms.goals, current.id)
    return ctx.comms.goals.todos.get(current.id), command


@pytest.mark.parametrize('reviewing', (False, True))
def test_released_claim_and_review_accept_a_distinct_successor_without_losing_history(
        held_commander, cited_source, reviewing):
    ctx = held_commander
    old = retained_claim(ctx, cited_source, reviewing)
    released, _ = release(ctx, old)
    unit = candidate(ctx)
    action = declared_successor(ctx, cited_source, released)
    goal = ctx.comms.goals.update_goal(unit.name, action)
    new = ctx.comms.goals.todos.get(old.id)
    assert new.held().owner_identity == unit.incarnation
    assert new.held().generation == goal.id
    assert old.held().owner_identity in new.held().previous_owners
    assert new.order.id != old.order.id
    assert new.order.run == old.order.run == RUN
    assert new.order.pull_requests == old.order.pull_requests
    assert new.strike_count == old.strike_count
    archived = new.history.events(new.id, CheckedProblemOrder)
    assert old.order in archived
    assert next(declaration for declaration in archived if declaration == old.order).claim == old.order.claim
    assert new.state == old.state
    assert new.order.run != MechanicalOrderRun()


@pytest.mark.parametrize('violation,error,reason', (
    ('live', TodoError, 'Only an undelivered order'),
    ('revision', TodoConflict, 'changed'),
    ('same-id', TodoError, 'distinct new order ID'),
    ('commander', NotOrderCommander, 'belongs to commander'),
    ('run', ToolRefusedError, 'retain the outstanding application run'),
))
def test_successor_refusals_leave_both_the_todo_and_candidate_goal_unchanged(
        held_commander, cited_source, violation, error, reason):
    ctx = held_commander
    old = retained_claim(ctx, cited_source, False)
    unit = candidate(ctx)
    current = old if violation == 'live' else release(ctx, old)[0]
    action = declared_successor(ctx, cited_source, current)
    if violation == 'revision':
        action = replace(action, expected_revision=old.revision)
    elif violation == 'same-id':
        action = replace(action, order=replace(action.order, id=old.order.id))
    elif violation == 'commander':
        actor = candidate(ctx, 'other-commander', 4.0)
        action = replace(action, assigner=actor.incarnation)
    elif violation == 'run':
        action = replace(action, order=replace(action.order, run=MechanicalOrderRun()))
    before = ctx.comms.registry.require(unit.name).goal
    with pytest.raises(error, match=reason):
        ctx.comms.goals.update_goal(unit.name, action)
    assert ctx.comms.goals.todos.get(old.id) == current
    assert ctx.comms.registry.require(unit.name).goal == before


def test_release_predecessor_and_reborn_assignee_are_not_substitutes(held_commander, cited_source):
    ctx = held_commander
    old = retained_claim(ctx, cited_source, True)
    released, command = release(ctx, old)
    unit = candidate(ctx)
    action = declared_successor(ctx, cited_source, released)
    wrong = replace(command, previous=replace(command.previous, generation=GoalId.mint()))
    with pytest.raises(TodoConflict):
        wrong.require_claim_successor(released, action.order)
    ctx.comms.registry.unregister(unit.name)
    ctx.comms.registry.remove(unit.name)
    replacement = candidate(ctx, unit.name, 5.0)
    assert replacement.incarnation != unit.incarnation
    before = replacement.goal
    with pytest.raises(TodoConflict):
        ctx.comms.goals.todos.assign((ExpectedProblem(released.id, released.revision),),
            owner=unit, assigner=ctx.incarnation, generation=GoalId.mint(),
            request=released.request.require(), review_at=action.review_at,
            order=action.order, traced=action.order.owner)
    assert ctx.comms.goals.todos.get(old.id) == released
    assert ctx.comms.registry.require(unit.name).goal == before


@pytest.mark.parametrize('claimed', (False, True))
def test_pending_handoff_and_same_claimant_correction_keep_their_existing_paths(
        held_commander, cited_source, claimed):
    ctx = held_commander
    old = retained_claim(ctx, cited_source, False) if claimed else ctx.comms.goals.todos.get('P1')
    unit = ctx.comms.registry.require('worker') if claimed else candidate(ctx)
    action = declared_successor(ctx, cited_source, old)
    goal = ctx.comms.goals.update_goal(unit.name, action)
    new = ctx.comms.goals.todos.get(old.id)
    assert new.held().owner_identity == unit.incarnation
    assert new.held().generation == goal.id
    assert new.order.id == action.order.id
    assert old.order in new.history.events(new.id, CheckedProblemOrder)
