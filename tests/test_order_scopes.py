"""Order scope is decoded once and owns its allowed delivery all the way to storage."""
from dataclasses import replace
import json
import sqlite3
from pathlib import Path

import pytest

from order_scope_fixture import FIX_SCOPE
from test_todos import order
from agent_comms.change_report import FileLine, MissingSourceLine
from agent_comms.commander.order import OrderFile
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import RequestGoalOrder
from agent_comms.installer.backup import StoreBackup
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsOrderDoneTool, CommsTodoCreateTool
from agent_comms.todos import (
    CommitDelivery, DeliverTodo, DoneTodoState, FixScope, FixShape, OrderScope,
    PendingProblemOrder, ReportDelivery, ReportedProblemOrder, TodoStore, TriageScope,
)
from cutover.order_scope import TRIAGE_PROBLEMS, convert


@pytest.mark.parametrize('scope', (TriageScope(), FIX_SCOPE))
def test_scope_round_trips_through_the_actual_order_file(tmp_path, scope):
    document = OrderFile(order=replace(order(tmp_path), scope=scope),
                         defect='The unit was given a fix without a declared parent',
                         why_stupid='The report and implementation require different delivery facts')
    assert OrderFile.read(json.dumps(FieldCodec.encode(document))) == document
    assert FieldCodec.decode(OrderScope, FieldCodec.encode(scope)) == scope


def test_an_order_without_scope_and_a_fix_without_parents_name_the_missing_field(tmp_path):
    wire = FieldCodec.encode(order(tmp_path))
    del wire['scope']
    with pytest.raises(MalformedRecord, match='scope'):
        FieldCodec.decode(PendingProblemOrder, wire)
    with pytest.raises(MalformedRecord, match='parents'):
        FieldCodec.decode(OrderScope, {'kind': 'fix', 'shape': {'parents': [], 'members': []}})


def test_shape_references_are_class_lines_not_names_or_decorators(tmp_path):
    location = FileLine.class_declaration(PendingProblemOrder)
    location.require_class(tmp_path)
    source = tmp_path / 'owner.py'
    source.write_text('value = 1\nclass Parent:\n    pass\nclass Member(Parent):\n    pass\n')
    shape = FixShape(parents=(FileLine('owner.py', 2),), members=(FileLine('owner.py', 4),))
    shape.require_references(tmp_path)
    with pytest.raises(MissingSourceLine, match='class declaration'):
        replace(shape, parents=(FileLine('owner.py', 1),)).require_references(tmp_path)


def assigned(comms, scope, problem='P1'):
    for name in ('lead', 'worker'):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(comms.root)))
    comms.goals.update_goal('lead', SetGoalAction(
        text='Classify the failing source or fix its declared parent', goal_id='request',
        order=RequestGoalOrder()))
    caller = ThreadCaller('lead')
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem=problem, order=replace(order(comms.root), scope=scope), request='request',
        review_at=RecordedEventTime(1000), commander='lead',
        repo=RepositoryName.from_wire('OpenHCSDev/agent-comms'),
        text='Classify the source without changing it',
    )).apply(comms, caller)
    return caller, comms.goals.todos.get(problem)


def test_triage_refuses_commit_before_git_or_storage_and_returns_its_report(comms):
    caller, current = assigned(comms, TriageScope())
    commit = CommitDelivery(commit='a' * 40, delivered_commit='a' * 40,
                            at=RecordedEventTime(1000))
    with pytest.raises(ValueError, match='scope triage'):
        CommsOrderDoneTool(problem=current.id, order_id=current.order.id,
                           delivery=commit, claims=()).apply(comms, caller)
    with pytest.raises(ValueError, match='scope triage'):
        DeliverTodo(current.revision, comms.registry.require(caller.name).incarnation, commit,
                    current.order.id).apply(current)
    assert comms.goals.todos.get(current.id) == current
    report = ReportDelivery(report='The command dispatch belongs on its declared parent; no source changed.')
    result = CommsOrderDoneTool(problem=current.id, order_id=current.order.id,
                               delivery=report, claims=()).apply(comms, caller)
    recorded = comms.goals.todos.get(current.id)
    assert result.todo == recorded
    assert recorded.state is DoneTodoState
    assert isinstance(recorded.order, ReportedProblemOrder)
    assert recorded.order.delivery.report == report.report
    assert recorded.assignment is None
    assert not recorded.order.waiting_on_install(recorded, {})
    assert 'triage' in current.order.instructions(current)
    assert 'Open the PR' not in current.order.instructions(current)


def test_a_fix_does_not_accept_a_report_instead_of_implementation(comms):
    caller, current = assigned(comms, FIX_SCOPE)
    with pytest.raises(ValueError, match='scope fix'):
        CommsOrderDoneTool(problem=current.id, order_id=current.order.id,
                           delivery=ReportDelivery(report='No implementation delivered'),
                           claims=()).apply(comms, caller)
    assert comms.goals.todos.get(current.id) == current


def test_one_shot_scopes_current_nested_and_prior_orders_on_a_copy(comms, tmp_path):
    _, original = assigned(comms, FIX_SCOPE)
    store = comms.goals.todos
    lead = comms.registry.require('lead')
    worker = comms.registry.require('worker')
    for problem in TRIAGE_PROBLEMS:
        store.create(problem, original.repo, 'Classify the source', creator=lead, owner=worker,
                     generation=original.held().generation, review_at=original.review_at,
                     goal=original.goal, order=replace(order(comms.root), scope=TriageScope()))
    copied = tmp_path / 'copy'
    StoreBackup.copy_tree(comms.root, copied)
    path = TodoStore.path_in(copied)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE todo SET order=json_remove(order, "$.scope")')
        prior = replace(original.order.checked(), id='prior-order')
        db.execute('UPDATE todo SET prior_orders=json_array(json_remove(?, "$.scope")) WHERE id=?',
                   (json.dumps(FieldCodec.encode(prior)), original.id))
        db.execute('UPDATE todo SET order=json_object("kind", "folded", "original", json(order), '
                   '"into", "P9", "why", "Merged findings") WHERE id=?', (original.id,))
    assert convert(copied) == len(TRIAGE_PROBLEMS) + 2
    converted = TodoStore(copied)
    for problem in TRIAGE_PROBLEMS:
        assert converted.get(problem).order.checked().scope == TriageScope()
    for declaration in converted.get(original.id).order_history:
        assert declaration.scope == FixScope(FixShape(parents=(declaration.owner,), members=()))
    assert convert(copied) == 0
    assert comms.goals.todos.get(original.id) == original
