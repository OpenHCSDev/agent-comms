"""One unit Goal reads its independent canonical Todo orders through real tools."""
import subprocess

import pytest

from agent_comms.change_report import FileLine
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction, OrderGoalAction, SetGoalAction
from agent_comms.goal_states import CompletedGoal
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import (CommsOrderDoneTool, CommsOrderTool, CommsProblemCloseTool,
    CommsTodoCreateTool, CommsTodoListTool, CommsTodoTool)
from agent_comms.todos import (CleanHunt, CommitDelivery, FirstCall, Hunt, OperationDelivery, OrderedHunt,
    NotifiedFirstCallEvent, ObservedFirstCallEvent, PendingProblemOrder, RecordFirstCallTodo,
    RecordHuntTodo, StrikeTodo, Todo)


@pytest.fixture
def commander(tmp_path):
    comms = Comms(tmp_path / 'wire')
    for created, name in enumerate(('lead', 'worker'), 1):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path), created_at=float(created)))
    comms.goals.update_goal('worker', SetGoalAction(text='Original commander brief', goal_id='ongoing'))
    return comms, ThreadCaller('lead')


def order(root, identity, command='printf 1', expected=1):
    return PendingProblemOrder(id=identity, owner=FileLine(__file__, 1),
        check=OrderCheck(str(root), command, CountCheckResult(expected)),
        edits=('Repair the existing owner',), banned=('No second store',), report='Report the result',
        at=RecordedEventTime(100), first_call_deadline=RecordedEventTime(220))


def create(commander, problem, command='printf 1', expected=1, deadline=1000):
    comms, caller = commander
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem=problem, order=order(comms.root, 'order-' + problem, command, expected),
        goal_id='ongoing', deadline=RecordedEventTime(deadline), commander='lead',
        repo='OpenHCSDev/agent-comms', text='Fix ' + problem,
    )).apply(comms, caller)
    return comms.goals.todos.get(problem)


def test_two_orders_keep_one_original_goal_and_independent_facts(commander):
    comms, caller = commander
    original = comms.registry.require('worker').goal
    first = create(commander, 'P1')
    second = create(commander, 'P2', 'printf 2', 2, deadline=2000)
    goal = comms.registry.require('worker').goal
    assert (goal.id, goal.text, goal.progress, goal.state) == (original.id, original.text, original.progress, original.state)
    assert first.assignment.generation == second.assignment.generation == goal.id
    assert first.deadline == RecordedEventTime(1000)
    assert second.deadline == RecordedEventTime(2000)
    assert first.order.check.result == CountCheckResult(1)
    assert second.order.check.result == CountCheckResult(2)
    assert first.order.first_call_deadline == RecordedEventTime(220)
    rows = comms.goals.todos.for_goal(comms.registry.require('worker').incarnation, goal.id)
    rendered = goal.instructions(rows)
    assert 'Problem: P1' in rendered and 'Problem: P2' in rendered
    assert 'printf 1' in rendered and 'printf 2' in rendered
    assert 'P1' not in goal.text and 'P2' not in goal.text
    assert len(CommsTodoListTool().apply(comms, caller)['todos']) == 2
    assert FieldCodec.decode(Todo, FieldCodec.encode(second)) == second


def test_existing_goal_reads_changed_todo_without_a_text_mirror(commander):
    comms, caller = commander
    todo = create(commander, 'P1')
    goal = comms.registry.require('worker').goal
    replacement = order(comms.root, 'replacement', 'printf 7', 7)
    CommsOrderTool(owner='worker', order=OrderGoalAction(problem='P1', order=replacement,
        goal_id=goal.id, deadline=RecordedEventTime(3000), commander='lead',
        expected_revision=todo.revision)).apply(comms, caller)
    assert comms.registry.require('worker').goal.text == 'Original commander brief'
    rows = comms.goals.todos.for_goal(comms.registry.require('worker').incarnation, goal.id)
    assert 'printf 7' in goal.instructions(rows)
    assert 'printf 1' not in goal.instructions(rows)
    assert rows[0].deadline == RecordedEventTime(3000)


def test_strike_time_and_clean_ordered_hunts_belong_to_problem(commander):
    comms, caller = commander
    todo = create(commander, 'P1')
    actor = comms.registry.require('lead').incarnation
    CommsTodoTool(id='P1', operation=StrikeTodo(todo.revision, actor, todo.assignment,
        'The assigned change broke the existing resource release path during review.', RecordedEventTime(500))).apply(comms, caller)
    current = comms.goals.todos.get('P1')
    assert current.strikes[0].at == RecordedEventTime(500)
    assert current.strikes[0].actor == actor
    why = 'This source reads the same rows on every event without changing any owned fact.'
    for hunt in (CleanHunt(FileLine(__file__, 1), why, RecordedEventTime(600)),
                 OrderedHunt(FileLine(__file__, 1), why, RecordedEventTime(700), 'order-P1')):
        current = comms.goals.todos.get('P1')
        CommsTodoTool(id='P1', operation=RecordHuntTodo(current.revision, actor, hunt)).apply(comms, caller)
        assert FieldCodec.decode(Hunt, FieldCodec.encode(hunt)) == hunt
    assert len(comms.goals.todos.get('P1').hunts) == 2


def test_delivery_checks_once_and_only_last_problem_completes_goal(commander):
    comms, caller = commander
    marker = comms.root / 'check-calls'
    create(commander, 'P1', f"printf x >> '{marker}'; printf 0; exit 1", 0)
    create(commander, 'P2')
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery()).apply(comms, caller)
    assert marker.read_text() == 'x'
    assert comms.registry.require('worker').goal.state.active
    assert not comms.goals.todos.get('P1').pending
    assert comms.goals.todos.get('P1').order.delivery.at.at > 0
    goal = comms.registry.require('worker').goal
    rows = comms.goals.todos.for_goal(comms.registry.require('worker').incarnation, goal.id)
    assert 'Problem: P1' not in goal.instructions(rows)
    assert 'Problem: P2' in goal.instructions(rows)
    CommsOrderDoneTool(problem='P2', delivery=OperationDelivery()).apply(comms, caller)
    assert isinstance(comms.registry.require('worker').goal.state, CompletedGoal)


def test_failed_check_keeps_problem_pending(commander):
    comms, caller = commander
    original = create(commander, 'P1', 'printf 9', 1)
    with pytest.raises(ValueError, match='expected last integer'):
        CommsOrderDoneTool(problem='P1', delivery=OperationDelivery()).apply(comms, caller)
    assert comms.goals.todos.get('P1') == original
    assert comms.registry.require('worker').goal.state.active


def test_close_records_evidence_once_and_does_not_depend_on_artifact_lifetime(commander):
    comms, caller = commander
    create(commander, 'P1')
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery()).apply(comms, caller)
    artifact = comms.root / 'installed-evidence'
    artifact.write_text('Verified installed path')
    CommsProblemCloseTool(problem='P1', works='The installed application now retains both queued orders through tab returns.',
        artifact=str(artifact)).apply(comms, caller)
    closed = comms.goals.todos.get('P1')
    assert closed.assignment is None and closed.state.declared_name == 'done'
    assert closed.order.delivery.at.at > 0
    artifact.unlink()
    assert comms.goals.todos.get('P1') == closed


def test_first_call_receipt_preserves_started_and_prior_alarm_without_json_flags(commander):
    comms, caller = commander
    todo = create(commander, 'P1')
    assert not todo.order.first_call.started and not todo.order.first_call.notified
    for event in (NotifiedFirstCallEvent, ObservedFirstCallEvent):
        todo = comms.goals.todos.get('P1')
        CommsTodoTool(id='P1', operation=RecordFirstCallTodo(todo.revision,
            comms.registry.require('lead').incarnation, event)).apply(comms, caller)
    call = comms.goals.todos.get('P1').order.first_call
    assert call.started and call.notified
    assert FieldCodec.decode(FirstCall, FieldCodec.encode(call)) == call
    assert call.observe() == call and call.notify() == call


def test_commit_delivery_records_the_actual_clean_commit_time(commander):
    comms, caller = commander
    def git(*args):
        return subprocess.check_output(('git', '-C', str(comms.root), *args), stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q')
    git('config', 'user.name', 'Isolated acceptance')
    git('config', 'user.email', 'acceptance@example.invalid')
    (comms.root / 'source.txt').write_text('delivered source')
    git('add', 'source.txt')
    git('commit', '-qm', 'Delivered fixture')
    commit = git('rev-parse', 'HEAD')
    create(commander, 'P1')
    CommsOrderDoneTool(problem='P1', delivery=CommitDelivery(commit=commit)).apply(comms, caller)
    delivery = comms.goals.todos.get('P1').order.delivery
    assert delivery.commit == commit
    assert delivery.at == RecordedEventTime(float(git('show', '-s', '--format=%ct', 'HEAD')))


def test_delivery_time_has_its_own_named_requirement():
    with pytest.raises(ValueError, match='delivery'):
        UnrecordedEventTime().require_delivery()
