"""Real order prepared and native write preflight share physical allocation edit ownership."""

from dataclasses import replace

import pytest

from agent_comms.errors import TodoConflict
from agent_comms.nominal.field_codec import GoalId, OrderId
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.order_tools import CommsTodoCreateTool
from agent_comms.repo import Checkout
from agent_comms.shell_effects import StorageDenied, WriteStorageEffect
from agent_comms.storage_rules import StorageProjection, StorageRule
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_model import DeclaredModel
from agent_comms.threads import Thread
from agent_comms.todos import Assignment, SharedWorktreeRefusal
from agent_comms.tool_caller import ThreadCaller

pytest_plugins = ('test_commander_forks',)


@pytest.fixture
def allocation_bus(fork_bus):
    comms, source, template = fork_bus
    for name in ('first', 'second'):
        comms.registry.declare(Thread(
            name, frozenset({'comms'}), Checkout(source.checkout),
            model=DeclaredModel('anthropic/claude-opus-4-6'), execution=ExternalThreadExecution))
    return comms, source, template


def issue(comms, source, template, name, problem):
    lead = comms.registry.require('lead').incarnation
    action = CreateProblemGoalAction(
        problem=problem, repo=source.github, text='Repair actual allocated source edit ownership',
        order=replace(template, id=OrderId.mint()),
        request=comms.goals.todos.get('P40').request.require(),
        review_at=template.at, commander=lead.name, assigner=lead)
    ThreadCaller('lead').invoke(CommsTodoCreateTool(order=action, owner=name), comms)
    return comms.goals.todos.get(problem)


def test_shared_launch_base_admits_distinct_unmaterialized_allocations_and_not_source_writes(allocation_bus):
    comms, source, template = allocation_bus
    first = issue(comms, source, template, 'first', 'P41')
    second = issue(comms, source, template, 'second', 'P42')
    one, two = (comms.registry.require(name) for name in ('first', 'second'))
    assert set(one.home_checkouts).intersection(two.home_checkouts)
    rule = StorageRule.load(comms.root)
    first_path = rule.order_checkout(rule.roots_of(one), first.id)
    second_path = rule.order_checkout(rule.roots_of(two), second.id)
    assert not first_path.exists() and not second_path.exists()
    assert first.held().owner_identity == one.incarnation
    assert second.held().owner_identity == two.incarnation
    # The shared launch/check repository is still not writable edit ownership for either holder.
    for owner in (one, two):
        with pytest.raises(StorageDenied):
            StorageProjection(rule, owner, comms).check((
                WriteStorageEffect(source.checkout / 'declaration.py'),))
    first_path.parent.mkdir(parents=True, exist_ok=True)
    source.git(source.checkout, 'worktree', 'add', '-q', '-b', 'allocated-first', str(first_path))
    StorageProjection(rule, one, comms).check((
        WriteStorageEffect(first_path / 'declaration.py'),))
    # Moving into an allocated home does not erase its declared edit permission.
    one = comms.registry.declare(replace(one, worktree=Checkout(first_path)))
    StorageProjection(rule, one, comms).check((
        WriteStorageEffect(first_path / 'declaration.py'),))
    with pytest.raises(StorageDenied):
        StorageProjection(rule, two, comms).check((
            WriteStorageEffect(first_path / 'declaration.py'),))


def test_physical_alias_refuses_public_creation_without_partial_goal_or_todo(allocation_bus):
    comms, source, template = allocation_bus
    first = issue(comms, source, template, 'first', 'P41')
    rule = StorageRule.load(comms.root)
    one, two = (comms.registry.require(name) for name in ('first', 'second'))
    original = rule.order_checkout(rule.roots_of(one), first.id)
    alias = rule.order_checkout(rule.roots_of(two), 'P42')
    original.mkdir(parents=True)
    alias.symlink_to(original, target_is_directory=True)
    before = two.goal
    with pytest.raises(SharedWorktreeRefusal) as refused:
        issue(comms, source, template, 'second', 'P42')
    assert refused.value.worktree == original.resolve()
    assert refused.value.holding_problem == first.id
    assert refused.value.holder == two.incarnation
    assert refused.value.holding_holder == one.incarnation
    assert comms.goals.todos.get(first.id) == first
    assert comms.registry.require('second').goal == before
    assert 'P42' not in {todo.id for todo in comms.goals.todos.list(source.github)}


def test_prepared_cut_refuses_a_replaced_assignment_generation(allocation_bus):
    comms, source, template = allocation_bus
    original = issue(comms, source, template, 'first', 'P41')
    owner = comms.registry.require('second').incarnation
    actor = comms.registry.require('lead').incarnation
    proposed = replace(original, assignment=Assignment.of(owner, actor, GoalId.mint()))
    store = comms.goals.todos
    prepared = store.preparing_edits((proposed,))
    other_generation = replace(proposed.held(), generation=GoalId.mint())
    from agent_comms.todos import IncomingWork
    with prepared.writing() as db:
        with pytest.raises(TodoConflict):
            other_generation.require_available_in(
                db, original.id, IncomingWork(frozenset({original.id}),
                                              frozenset({original.order.id})),
                comms.registry.snapshot(), prepared.prepared_edits())
    assert store.get(original.id) == original


def test_same_owner_incoming_group_can_share_its_own_physical_allocation(allocation_bus):
    from agent_comms.todos import IncomingWork
    comms, source, template = allocation_bus
    first = issue(comms, source, template, 'first', 'P41')
    second = issue(comms, source, template, 'second', 'P42')
    owner = comms.registry.require('first')
    actor = comms.registry.require('lead').incarnation
    proposed = replace(second, assignment=Assignment.of(owner.incarnation, actor, first.held().generation))
    rule = StorageRule.load(comms.root)
    original = rule.order_checkout(rule.roots_of(owner), first.id)
    alias = rule.order_checkout(rule.roots_of(owner), second.id)
    original.mkdir(parents=True)
    alias.symlink_to(original, target_is_directory=True)
    prepared = comms.goals.todos.preparing_edits((proposed,))
    with prepared.writing() as db:
        proposed.held().require_available_in(
            db, second.id, IncomingWork(frozenset({first.id, second.id}),
                                       frozenset({first.order.id, second.order.id})),
            comms.registry.snapshot(), prepared.prepared_edits())


def test_recreated_holder_cannot_use_the_prepared_birth(allocation_bus):
    from agent_comms.todos import IncomingWork
    comms, source, template = allocation_bus
    current = issue(comms, source, template, 'first', 'P41')
    old = comms.registry.require('second')
    actor = comms.registry.require('lead').incarnation
    proposed = replace(current, assignment=Assignment.of(old.incarnation, actor, GoalId.mint()))
    prepared = comms.goals.todos.preparing_edits((proposed,))
    comms.registry.remove(old.name)
    comms.registry.declare(Thread(
        old.name, old.tags, Checkout(source.checkout),
        model=old.model, execution=ExternalThreadExecution))
    assert comms.registry.require(old.name).incarnation != old.incarnation
    with prepared.writing() as db:
        with pytest.raises(TodoConflict):
            proposed.held().require_available_in(
                db, current.id, IncomingWork(frozenset({current.id}),
                                            frozenset({current.order.id})),
                comms.registry.snapshot(), prepared.prepared_edits())
    assert comms.goals.todos.get(current.id) == current
