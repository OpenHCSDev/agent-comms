"""Order scope is decoded once and owns its allowed delivery all the way to storage."""
from dataclasses import replace
import json

import pytest

from order_scope_fixture import FIX_SCOPE
from test_todos import order
from source_revision_fixture import SourceRepository
from agent_comms.change_report import FileLine, MissingSourceLine, PullRequestHeadRevision
from agent_comms.check_root import CheckRoot
from agent_comms.commander.order import OrderFile
from cli_command import core
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsOrderDoneTool, CommsTodoCreateTool
from agent_comms.todos import (
    CommitDelivery, DoneTodoState, FixShape, OrderScope,
    PendingProblemOrder, ReportDelivery, ReportedProblemOrder, TriageScope,
)


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
    request = comms.goals.todos.add_request(comms.registry.require('lead').incarnation,
        'Classify the failing source or fix its declared parent').id
    caller = ThreadCaller('lead')
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem=problem, order=replace(order(source), scope=scope), request=request,
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
        result = core('--root', str(copied.root), 'tool', '--from', 'lead',
                      '--tool', request.declared_name, '--arguments', json.dumps(request.arguments))
        assert result.returncode == 0, result.stderr
        assert report in result.stdout
        assert copied.goals.todos.get(current.id).state is DoneTodoState
    assert comms.goals.todos.get('P1').pending
