"""Order scope is decoded once and owns its allowed delivery all the way to storage."""
from dataclasses import replace
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from order_scope_fixture import FIX_SCOPE
from test_todos import order
from source_revision_fixture import SourceRepository
from agent_comms.change_report import FileLine, MissingSourceLine, PullRequestHeadRevision
from agent_comms.check_root import CheckRoot
from agent_comms.commander.order import OrderFile
from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import RequestGoalOrder
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.carry import Carried, CarryChain
from agent_comms.locked_store import SQLiteFile
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsOrderDoneTool, CommsTodoCreateTool
from agent_comms.todos import (
    CommitDelivery, DoneTodoState, FixScope, FixShape, FoldedProblemOrder, OrderScope,
    PendingProblemOrder, ReportDelivery, ReportedProblemOrder, TodoStore, TriageScope,
)
from cutover.carry_order_scopes import TRIAGE_PROBLEMS, OrderScopes


@pytest.mark.parametrize('scope', (TriageScope(), FIX_SCOPE))
def test_scope_round_trips_through_the_actual_order_file(tmp_path, scope, source_repository):
    document = OrderFile(order=replace(order(source_repository), scope=scope),
                         defect='The unit was given a fix without a declared parent',
                         why_stupid='The report and implementation require different delivery facts')
    assert OrderFile.read(json.dumps(FieldCodec.encode(document))) == document
    assert FieldCodec.decode(OrderScope, FieldCodec.encode(scope)) == scope


def test_an_order_without_scope_and_a_fix_without_parents_name_the_missing_field(tmp_path, source_repository):
    wire = FieldCodec.encode(order(source_repository))
    del wire['scope']
    with pytest.raises(MalformedRecord, match='scope'):
        FieldCodec.decode(PendingProblemOrder, wire)
    with pytest.raises(MalformedRecord, match='parents'):
        FieldCodec.decode(OrderScope, {'kind': 'fix', 'shape': {'parents': [], 'members': []}})


def test_shape_references_are_class_lines_not_names_or_decorators(tmp_path):
    source = SourceRepository.create(tmp_path)
    revision = PullRequestHeadRevision(source.link)
    shape = FixShape(parents=(FileLine('owner.py', 2),), members=(FileLine('owner.py', 4),))
    shape.require_references(source.checkout, revision)
    for location in (FileLine('owner.py', 1), FileLine('branch.py', 1)):
        with pytest.raises(MissingSourceLine, match='class declaration'):
            replace(shape, parents=(location,)).require_references(source.checkout, revision)


def assigned(comms, scope, source, problem='P1'):
    for name in ('lead', 'worker'):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(comms.root)))
    return requested(comms, scope, source, problem)


def requested(comms, scope, source, problem='P1'):
    comms.goals.update_goal('lead', SetGoalAction(
        text='Classify the failing source or fix its declared parent', goal_id='request',
        order=RequestGoalOrder()))
    caller = ThreadCaller('lead')
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem=problem, order=replace(order(source), scope=scope), request='request',
        review_at=RecordedEventTime(1000), commander='lead',
        assigner=comms.registry.require('lead').incarnation,
        repo=RepositoryName.from_wire('OpenHCSDev/agent-comms'),
        text='Classify the source without changing it',
    )).apply(comms, caller)
    return caller, comms.goals.todos.get(problem)


def test_triage_returns_its_report_without_fictitious_installation(comms, source_repository):
    caller, current = assigned(comms, TriageScope(), source_repository)
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


@pytest.mark.parametrize('scope,wrong_delivery', (
    (TriageScope(), CommitDelivery(commit='a' * 40, delivered_commit='a' * 40)),
    (FIX_SCOPE, ReportDelivery(report='Classification is not an implementation')),
))
def test_scope_accepts_only_its_declared_delivery_at_native_decode(comms, scope, wrong_delivery, source_repository):
    caller, current = assigned(comms, scope, source_repository)
    request = CommsOrderDoneTool(problem=current.id, order_id=current.order.id,
                                 delivery=wrong_delivery, claims=())
    with pytest.raises(ValueError, match='delivery'):
        CommsOrderDoneTool.invoke(comms, request.arguments, caller=caller)
    assert comms.goals.todos.get(current.id) == current


def test_report_returns_through_the_real_cli_on_a_selected_history_copy(comms, tmp_path, source_repository):
    assigned(comms, TriageScope(), source_repository)
    comms.registry.storage_rule = replace(comms.registry.storage_rule, scratch_base=tmp_path)
    with CheckRoot(comms, ('lead', 'worker'), tmp_path / 'journey' / 'copied').open() as copied:
        _, current = requested(copied, TriageScope(), source_repository)
        report = 'The copied native order needs classification only; no source was changed.'
        request = CommsOrderDoneTool(problem=current.id, order_id=current.order.id,
                                     delivery=ReportDelivery(report=report), claims=())
        result = subprocess.run(
            (sys.executable, '-m', 'agent_comms.cli', '--root', str(copied.root), 'tool',
             '--from', 'lead', '--tool', request.declared_name,
             '--arguments', json.dumps(request.arguments)),
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
            capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert report in result.stdout
        assert copied.goals.todos.get(current.id).state is DoneTodoState
    assert comms.goals.todos.get('P1').pending


def test_carry_scopes_current_nested_and_prior_orders_on_a_copy(comms, tmp_path, source_repository):
    assert OrderScopes in CarryChain.at(Path(__file__).resolve().parents[1]).pending(())
    _, original = assigned(comms, FIX_SCOPE, source_repository)
    store = comms.goals.todos
    lead = comms.registry.require('lead')
    worker = comms.registry.require('worker')
    for problem in TRIAGE_PROBLEMS:
        store.create(problem, original.repo, 'Classify the source', creator=lead, owner=worker,
                     assigner=lead.incarnation,
                     generation=original.held().generation, review_at=original.review_at,
                     goal=original.goal, order=replace(order(source_repository), scope=TriageScope()))
    copied = tmp_path / 'copy'
    StoreBackup.copy_tree(comms.root, copied)
    path = TodoStore.path_in(copied)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE todo SET "order"=json_remove("order", ?)', ('$.scope',))
        prior = replace(original.order.checked(), id='prior-order')
        db.execute('UPDATE todo SET prior_orders=json_array(json_remove(?, ?)) WHERE id=?',
                   (json.dumps(FieldCodec.encode(prior)), '$.scope', original.id))
        folded = FoldedProblemOrder(original=original.order, into='P9', why='Merged findings')
        db.execute('UPDATE todo SET "order"=json_remove(?, ?) WHERE id=?',
                   (json.dumps(FieldCodec.encode(folded)), '$.original.scope', original.id))
    before = CoordinationStore.digest(path)
    migration = OrderScopes(copied, comms.root, tmp_path / 'spill', False)
    dry_run = migration.carry()
    assert dry_run == Carried(len(TRIAGE_PROBLEMS) + 2)
    assert CoordinationStore.digest(path) == before
    migration = replace(migration, apply=True)
    applied = migration.carry()
    assert applied == Carried(len(TRIAGE_PROBLEMS) + 2, rewritten=tuple(
        str(file.relative_to(copied)) for file in SQLiteFile.files(path)))
    converted = TodoStore(copied)
    for problem in TRIAGE_PROBLEMS:
        assert converted.get(problem).order.checked().scope == TriageScope()
    for declaration in converted.get(original.id).order_history:
        assert declaration.scope == FixScope(FixShape(parents=(declaration.owner,), members=()))
    assert migration.carry() == Carried(0)
    assert comms.goals.todos.get(original.id) == original
