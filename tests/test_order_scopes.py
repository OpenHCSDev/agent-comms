"""Order scope is decoded once and owns its allowed delivery all the way to storage."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.errors import MissingSourceLine
from agent_comms.execution.check_root import CheckRoot
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord
from agent_comms.planning.goal_actions import CreateProblemGoalAction
from agent_comms.planning.goals import OrderCheck
from agent_comms.planning.held_orders import HeldOrders
from agent_comms.planning.orders import (
    CommitDelivery,
    DoneTodoState,
    OrderScope,
    PendingProblemOrder,
    ReportDelivery,
    ReportedProblemOrder,
    TodoStore,
    TriageScope,
)
from agent_comms.planning.problem_findings import FixShape
from agent_comms.planning.pull_requests import PullRequestHeadRevision
from agent_comms.projects.change_report import FileLine
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.registry.registry_document import NoTeamProject, RegisteredTeamProject
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.team.order import OrderFile
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.orders import (
    CommsOrderDoneTool,
    CommsProblemNumberTool,
    CommsTodoCreateTool,
)
from cli_command import core
from order_scope_fixture import FIX_SCOPE
from source_revision_fixture import SourceRepository
from test_todos import order


@pytest.mark.parametrize('scope', (TriageScope(), FIX_SCOPE))
def test_scope_round_trips_through_the_actual_order_file(tmp_path, scope, source_repository):
    document = OrderFile(order=replace(order(source_repository), scope=scope),
                         defect='The teammate was given a fix without a declared parent',
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
    shape.require_references(source.roots, revision)
    for location in (FileLine('owner.py', 1), FileLine('branch.py', 1)):
        with pytest.raises(MissingSourceLine, match='class declaration'):
            replace(shape, parents=(location,)).require_references(source.roots, revision)


def assigned(comms, scope, source, problem='P1', edits=('Fix the problem',)):
    for name in ('colleague', 'worker'):
        comms.registry.declare(NativeThread(name, frozenset(), Checkout(comms.root)))
    return requested(comms, scope, source, problem, edits)


def requested(comms, scope, source, problem='P1', edits=('Fix the problem',), owner='worker'):
    request = comms.goals.todos.add_request(comms.registry.require('colleague').incarnation,
        'Classify the failing source or fix its declared parent').id
    caller = ThreadCaller('colleague')
    CommsTodoCreateTool(owner=owner, order=CreateProblemGoalAction(
        problem=problem, order=replace(order(source), scope=scope, edits=edits), request=request,
        review_at=RecordedEventTime(1000),
        assigner=comms.registry.require('colleague').incarnation,
        repo=source.github,
        text='Classify the source without changing it',
    )).apply(comms, caller)
    return caller, comms.goals.todos.get(problem)


def test_held_orders_recall_holder_problem_scope_and_last_edit_from_the_store(
    private_comms, source_repository,
):
    assigned(private_comms, FIX_SCOPE, source_repository, 'P1',
             ('Reproduce the bounce first', 'Then stop and report; no src edit before it'))
    # A teammate holding an order takes no unrelated one, so the triage goes to another.
    (private_comms.root / 'triager').mkdir()
    private_comms.registry.declare(
        NativeThread('triager', frozenset(), Checkout(private_comms.root / 'triager')))
    requested(private_comms, TriageScope(), source_repository, 'P2',
              ('Classify the failing source',), owner='triager')
    held = HeldOrders.of(TodoStore(private_comms.root), private_comms.registry.snapshot())
    assert sorted(hold.recalled() for hold in held.live) == [
        'triager P2 triage: Classify the failing source',
        'worker P1 fix: Then stop and report; no src edit before it',
    ]


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
    with CheckRoot(comms, ('colleague', 'worker'), tmp_path / 'journey' / 'copied').open() as copied:
        _, current = requested(copied, TriageScope(), source_repository)
        report = 'The copied native order needs classification only; no source was changed.'
        request = CommsOrderDoneTool(problem=current.id, order_id=current.order.id,
                                     delivery=ReportDelivery(report=report), claims=())
        result = core('--root', str(copied.root), 'tool', '--from', 'colleague',
                      '--tool', request.declared_name, '--arguments', json.dumps(request.arguments))
        assert result.returncode == 0, result.stderr
        assert report in result.stdout
        assert copied.goals.todos.get(current.id).state is DoneTodoState
    assert comms.goals.todos.get('P1').pending


def test_a_check_named_by_repository_runs_in_its_registered_checkout(tmp_path):
    """A cloud session cannot know the host path, so it names the repository; the order binds
    it where it enters, as `order send` does. Before, comms_todo_create kept "agent-comms" and
    git failed to cd into it inside the connector, an HTTP 500 the client saw as a lost
    connection."""
    core = Repo(checkout=tmp_path / "agent-comms", github=RepositoryName.from_wire("OpenHCSDev/agent-comms"))
    toad = Repo(checkout=tmp_path / "toad", github=RepositoryName.from_wire("OpenHCSDev/toad"))
    project = RegisteredTeamProject(repos=(core, toad))
    assert OrderCheck(Path("OpenHCSDev/toad"), "true").bound_in(project).directory == toad.checkout
    assert OrderCheck(core.checkout, "true").bound_in(project).directory == core.checkout
    with pytest.raises(ValueError):
        OrderCheck(Path("nowhere"), "true").bound_in(project)
    # A bus with no team project keeps the written directory, as before.
    assert OrderCheck(tmp_path / "x", "true").bound_in(NoTeamProject()).directory == tmp_path / "x"


def test_reserved_problem_numbers_never_collide_and_create_cleanly(comms, source_repository):
    """comms_todo_create made each caller pick a number, so concurrent resends raced for
    the same one (a TodoConflict). comms_problem_number reserves through the allocation
    order send already uses: two callers get two numbers, and the reserved one creates."""
    for name in ('colleague', 'worker'):
        comms.registry.declare(NativeThread(name, frozenset(), Checkout(comms.root)))
    caller = ThreadCaller('colleague')
    first = CommsProblemNumberTool().apply(comms, caller).problem
    second = CommsProblemNumberTool().apply(comms, caller).problem
    assert first != second
    _, created = requested(comms, TriageScope(), source_repository, first, ('Classify it',))
    assert created.id == first
    assert CommsProblemNumberTool().apply(comms, caller).problem not in (first, second)
