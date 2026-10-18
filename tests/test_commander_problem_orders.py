"""One unit Goal reads its independent canonical Todo orders through real tools."""
import importlib
import os
import subprocess

import pytest
from dataclasses import replace

from agent_comms.change_report import FileLine
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction, OrderGoalAction, SetGoalAction
from agent_comms.goal_states import CompletedGoal
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import (CommsOrderCheckTool, CommsOrderDoneTool, CommsOrderTool, CommsProblemCloseTool,
    CommsTodoCreateTool, CommsTodoListTool, CommsTodoTool)
from agent_comms.todos import (AwaitingFirstCall, GoalRef, CleanHunt, CommitDelivery, FirstCall, Hunt, OrderDelivery,
    RecordedCommitDelivery, MechanicalOrderRun,
    LateObservedFirstCall, OperationDelivery, OrderedHunt,
    NotifiedFirstCallEvent, ObservedFirstCallEvent, PendingProblemOrder, RecordFirstCallTodo,
    Assignment, DoneTodoState, RecordHuntTodo, ReleaseTodo, StrikeReason, StrikeTodo, Todo, TodoConflict, VoidStrikeTodo)


def order(root, identity, command='printf 1', expected=1):
    return PendingProblemOrder(id=identity, owner=FileLine(__file__, 1), run=MechanicalOrderRun(),
        check=OrderCheck(str(root), command, CountCheckResult(expected)),
        edits=('Repair the existing owner',), banned=('No second store',), report='Report the result',
        at=RecordedEventTime(100), first_call_deadline=RecordedEventTime(220))


def create(commander, problem, command='printf 1', expected=1, deadline=1000):
    comms, caller = commander
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem=problem, order=order(comms.root, 'order-' + problem, command, expected),
        request='request', deadline=RecordedEventTime(deadline), commander='lead',
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
    assert len(CommsTodoListTool().apply(comms, caller).todos) == 2
    assert FieldCodec.decode(Todo, FieldCodec.encode(second)) == second


def test_existing_goal_reads_changed_todo_without_a_text_mirror(commander):
    comms, caller = commander
    todo = create(commander, 'P1')
    goal = comms.registry.require('worker').goal
    replacement = order(comms.root, 'replacement', 'printf 7', 7)
    CommsOrderTool(owner='worker', order=OrderGoalAction(problem='P1', order=replacement,
        request='request', deadline=RecordedEventTime(3000), commander='lead',
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


def test_first_call_state_retains_observation_and_prior_alarm(commander):
    comms, caller = commander
    todo = create(commander, 'P1')
    assert isinstance(todo.order.first_call, AwaitingFirstCall)
    for event in (NotifiedFirstCallEvent, ObservedFirstCallEvent):
        todo = comms.goals.todos.get('P1')
        CommsTodoTool(id='P1', operation=RecordFirstCallTodo(todo.revision,
            comms.registry.require('lead').incarnation, event)).apply(comms, caller)
    call = comms.goals.todos.get('P1').order.first_call
    assert isinstance(call, LateObservedFirstCall)
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
    git('update-ref', 'refs/remotes/origin/main', commit)
    create(commander, 'P1')
    CommsOrderDoneTool(problem='P1', delivery=CommitDelivery(commit=commit, delivered_commit=commit)).apply(comms, caller)
    delivery = comms.goals.todos.get('P1').order.delivery
    assert delivery.commit == delivery.delivered == commit
    assert delivery.at == RecordedEventTime(float(git('show', '-s', '--format=%ct', 'HEAD')))


def test_lateness_reads_the_delivered_commit_not_the_checked_merge(commander, commander_script):
    comms, caller = commander
    script, _ = commander_script

    def git(*args, at=None):
        env = {**os.environ, 'GIT_AUTHOR_DATE': f'@{at} +0000', 'GIT_COMMITTER_DATE': f'@{at} +0000'} if at else None
        return subprocess.check_output(('git', '-C', str(comms.root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                                        *args), env=env, stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q', '-b', 'main')
    git('commit', '-q', '--allow-empty', '-m', 'base', at=500)
    git('checkout', '-q', '-b', 'work')
    git('commit', '-q', '--allow-empty', '-m', 'delivered before the deadline', at=1000)
    delivered = git('rev-parse', 'HEAD')
    git('checkout', '-q', 'main')
    git('merge', '-q', '--no-ff', '-m', 'merged after the deadline', 'work', at=5000)
    merge = git('rev-parse', 'HEAD')
    create(commander, 'P1', deadline=2000)
    create(commander, 'P2', deadline=2000)
    with pytest.raises(ValueError, match='no origin branch'):
        CommsOrderDoneTool(problem='P1', delivery=CommitDelivery(commit=merge, delivered_commit=delivered)).apply(comms, caller)
    git('update-ref', 'refs/remotes/origin/work', delivered)
    script.done('P1', merge, delivered)
    met = comms.goals.todos.get('P1')
    assert (met.order.delivery.delivered, met.order.delivery.at) == (delivered, RecordedEventTime(1000))
    assert script.struck(met, 'worker') == 0

    git('checkout', '-q', '-b', 'side', delivered)
    git('commit', '-q', '--allow-empty', '-m', 'never merged', at=1500)
    side = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/side', side)
    git('checkout', '-q', 'main')
    git('commit', '-q', '--allow-empty', '-m', 'delivered after the deadline', at=3000)
    late = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', late)
    with pytest.raises(ValueError, match='not in the checked commit'):
        CommsOrderDoneTool(problem='P2', delivery=CommitDelivery(commit=late, delivered_commit=side)).apply(comms, caller)
    script.done('P2', late, late)
    struck = comms.goals.todos.get('P2')
    assert script.struck(struck, 'worker') == 1 and late in struck.strikes[-1].reason


def test_a_done_record_from_before_the_split_reads_its_one_commit_as_both():
    old = {'kind': 'commit', 'commit': 'd012d23b4', 'at': FieldCodec.encode(RecordedEventTime(1000))}
    recorded = FieldCodec.decode(OrderDelivery, old)
    assert type(recorded) is RecordedCommitDelivery and recorded.delivered == recorded.commit == 'd012d23b4'
    assert FieldCodec.encode(recorded) == old


def test_delivery_time_has_its_own_named_requirement():
    with pytest.raises(ValueError, match='delivery'):
        UnrecordedEventTime().require_delivery()


def test_commander_script_records_every_order_step_in_core(commander, commander_script):
    """problem, send, strike, hunt, done and close each change the Todo in Core; no order state is written to files."""
    comms, _ = commander
    script, repo = commander_script
    git = lambda *a: subprocess.run(['git', '-C', str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()
    (repo / 'count.txt').write_text('0\n')
    git('add', '.')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'base')
    body = repo.parent / 'order.txt'
    body.write_text(f"Problem: NEW\nOwner: {__file__}:1\nCheck: {repo} :: cat count.txt -> 1\n"
                    "Works when: the installed app shows the counter at one after restart\n"
                    "The counter is wrong and that is stupid.\n1. Set count.txt to 1.\n"
                    "Banned: no second store; no cache\nReport: the commit and the count before -> after\n")
    due = '23:59'
    script.problem('The counter file holds zero where the fleet needs one', 'worker', due, str(body), 'request')
    created = comms.goals.todos.get('P1')
    assert (created.assignment.owner, created.order.edits, created.order.banned) == (
        'worker', ('Set count.txt to 1.',), ('no second store', 'no cache'))

    script.send('worker', due, str(body), 'request')  # the same holder again without --clean is a send-back
    sent_back = comms.goals.todos.get('P1')
    assert sent_back.order.id != created.order.id and script.struck(sent_back, 'worker') == 1
    judge = importlib.reload(importlib.import_module('agent_comms.commander.judge'))
    assert 'Set count.txt to 1.' in judge.my_messages('worker')[0]['text']

    script.hunt(f'{__file__}:1', 'the counter is reread per event and the order replaces it with one read', sent_back.order.id)
    assert [h.sent for h in comms.goals.todos.get('P1').hunts] == [sent_back.order.id]

    (repo / 'count.txt').write_text('1\n')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'fix')
    git('update-ref', 'refs/remotes/origin/main', 'HEAD')
    script.done('P1', git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD'))
    assert not comms.goals.todos.get('P1').pending

    artifact = repo.parent / 'seen.png'
    artifact.write_text('x')
    script.close('P1', 'the installed app shows the counter at one after restart', str(artifact))
    assert not comms.goals.todos.get('P1').state.open and script.open_todos() == []
    assert not {'problems.json', 'deadlines.json', 'closed.jsonl', 'strikes.jsonl', 'done.jsonl',
                'sent.jsonl', 'hunts.jsonl'} & {
        path.name for path in (repo.parent / 'state').iterdir()}


def test_order_check_replaces_a_wrong_check_so_done_runs_the_corrected_one(commander, commander_script):
    """A wrong check the commander wrote is corrected on the open order; its id, holder, deadline and strikes stay."""
    comms, _ = commander
    script, repo = commander_script
    git = lambda *a: subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', *a],
                                    check=True, capture_output=True, text=True).stdout.strip()
    (repo / 'count.txt').write_text('0\n')
    git('add', '.')
    git('commit', '-qm', 'base')
    git('update-ref', 'refs/remotes/origin/main', 'HEAD')
    body = repo.parent / 'order.txt'
    body.write_text(f"Problem: NEW\nOwner: {__file__}:1\nCheck: {repo} :: grep -c two count.txt -> 1\n"
                    "Works when: the installed app shows the counter at one after restart\n"
                    "The counter is wrong and that is stupid.\n1. Set count.txt to 1.\n"
                    "Banned: no second store; no cache\nReport: the commit and the count before -> after\n")
    script.problem('The counter file holds zero where the fleet needs one', 'worker', '23:59', str(body), 'request')
    script.strike('worker', 'P1 was sent back once before its check was corrected')
    sent = comms.goals.todos.get('P1')
    corrected = f'{repo} :: cat count.txt -> 1'
    why = 'the recorded check greps a word the ordered edit never writes'
    with pytest.raises(SystemExit, match='does not pass undelivered work'):
        script.correct_check('P1', corrected, why)  # main still holds 0
    with pytest.raises(TodoConflict):
        CommsOrderCheckTool(problem='P1', check=OrderCheck(str(repo), 'cat count.txt', CountCheckResult(1)),
                            why=why).apply(comms, ThreadCaller('worker'))
    assert comms.goals.todos.get('P1') == sent

    (repo / 'count.txt').write_text('1\n')
    git('commit', '-qam', 'fix')
    git('update-ref', 'refs/remotes/origin/main', 'HEAD')
    fixed = git('rev-parse', 'HEAD')
    with pytest.raises(ValueError, match='expected last integer'):
        script.done('P1', fixed, fixed)
    script.correct_check('P1', corrected, why)
    current = comms.goals.todos.get('P1')
    assert (current.order.id, current.assignment, current.deadline, current.strikes) == (
        sent.order.id, sent.assignment, sent.deadline, sent.strikes)
    [record] = current.order.check_corrections
    assert (record.previous, record.check, record.reason) == (
        sent.order.check, OrderCheck(str(repo), 'cat count.txt', CountCheckResult(1)), why)
    script.done('P1', fixed, fixed)
    assert not comms.goals.todos.get('P1').pending
    assert comms.goals.todos.get('P1').order.check_corrections == (record,)


def test_commander_refuses_jargon_before_core_records_the_problem(commander_script):
    """A refused outgoing text leaves Core without the problem, so "nothing recorded" is true."""
    script, repo = commander_script
    (repo / 'count.txt').write_text('0\n')
    body = repo.parent / 'order.txt'
    body.write_text(f"Problem: NEW\nOwner: {__file__}:1\nCheck: {repo} :: cat count.txt -> 1\n"
                    "Works when: the installed app shows the counter at one after restart\n"
                    "The counter custody is wrong and that is stupid.\n1. Set count.txt to 1.\n"
                    "Banned: no second store; no cache\nReport: the commit and the count before -> after\n")
    with pytest.raises(SystemExit, match='^REFUSED, nothing recorded'):
        script.problem('The counter file holds zero where the fleet needs one', 'worker', '23:59', str(body), 'request')
    assert script.open_todos() == [] and 'Problem: NEW' in body.read_text()


def test_commander_refuses_a_check_that_prints_no_count(commander_script):
    """done could never pass a check that prints no integer, so send refuses it before Core records anything."""
    script, repo = commander_script
    body = repo.parent / 'order.txt'
    body.write_text(f"Problem: NEW\nOwner: {__file__}:1\nCheck: {repo} :: git grep -c no-such-text -> 0\n"
                    "Works when: the installed app shows the counter at one after restart\n"
                    "The counter is wrong and that is stupid.\n1. Set count.txt to 1.\n"
                    "Banned: no second store; no cache\nReport: the commit and the count before -> after\n")
    with pytest.raises(SystemExit, match='the check printed no integer'):
        script.problem('The counter file holds zero where the fleet needs one', 'worker', '23:59', str(body), 'request')
    assert script.open_todos() == []


def test_a_voided_strike_stays_readable_stops_counting_and_voids_once(commander):
    """Only the commander voids; a void needs a live strike on this todo; a closed todo is voided alike."""
    comms, caller = commander
    todo = create(commander, 'P1')
    lead = comms.registry.require('lead').incarnation
    CommsTodoTool(id='P1', operation=StrikeTodo(todo.revision, lead, todo.assignment,
        'The unit delivered its order after the recorded deadline had already passed.')).apply(comms, caller)
    struck = comms.goals.todos.get('P1')
    strike, = struck.strikes
    assert struck.strike_count == 1 and struck.live_strikes('worker') == (strike,)
    why = 'The commander recorded the later main merge as the delivery, so this lateness strike was wrong.'
    worker = comms.registry.require('worker').incarnation
    with pytest.raises(TodoConflict):
        VoidStrikeTodo(struck.revision, worker, strike, why).apply(struck)
    closed = replace(struck, state=DoneTodoState)
    assert VoidStrikeTodo(struck.revision, lead, strike, why).apply(closed).live_strikes() == ()
    CommsTodoTool(id='P1', operation=VoidStrikeTodo(struck.revision, lead, strike, why)).apply(comms, caller)
    voided = comms.goals.todos.get('P1')
    assert voided.strikes == (strike,) and voided.voided[0].strike == strike
    assert voided.strike_count == 0 and voided.live_strikes('worker') == ()
    with pytest.raises(TodoConflict):
        VoidStrikeTodo(voided.revision, lead, strike, why).apply(voided)
    assert FieldCodec.decode(Todo, FieldCodec.encode(voided)) == voided


def test_order_unstrike_voids_the_newest_live_strike_on_the_record(commander, commander_script, tmp_path, monkeypatch):
    comms, _ = commander
    script, repo = commander_script
    create(commander, 'P1')
    posted = []
    monkeypatch.setattr(script, 'post', lambda recipients, body: posted.append((recipients, body)))
    script.strike('worker', 'P1 was struck for lateness against the wrong delivered commit')
    script.unstrike('P1', 'P1 was delivered before its deadline; the commander recorded the later merge as the delivery')
    current = comms.goals.todos.get('P1')
    assert len(current.strikes) == 1 and script.struck(current, 'worker') == 0
    assert 'strike voided on P1' in (tmp_path / 'narrative.md').read_text()
    assert posted[-1][0] == ['#comms'] and 'strike voided on P1' in posted[-1][1]
    with pytest.raises(SystemExit, match='no live strike'):
        script.unstrike('P1', 'P1 has no strike left to take back after the first void was recorded')


FIRED = 'The unit took two strikes on this problem and its second delivery was late again.'


@pytest.mark.parametrize('caller, why, refused', [
    ('lead', FIRED, None),
    ('worker', FIRED, pytest.raises(TodoConflict)),
    ('lead', 'Late twice.', pytest.raises(ValueError, match='plainly in 10')),
])
def test_only_the_commander_releases_a_problem_and_its_owner_stays_on_record(commander, caller, why, refused):
    comms, _ = commander
    todo = create(commander, 'P1')
    CommsTodoTool(id='P1', operation=StrikeTodo(todo.revision, comms.registry.require('lead').incarnation,
        todo.assignment, FIRED)).apply(comms, commander[1])
    struck = comms.goals.todos.get('P1')
    def release():
        CommsTodoTool(id='P1', operation=ReleaseTodo(struck.revision, comms.registry.require(caller).incarnation,
            struck.assignment, StrikeReason(why))).apply(comms, ThreadCaller(caller))
    if refused:
        with refused:
            release()
        assert comms.goals.todos.get('P1') == struck
        return
    release()
    released = comms.goals.todos.get('P1')
    assert released.assignment is None and released.revision == struck.revision + 1
    assert (released.order, released.strikes) == (struck.order, struck.strikes)
    assert released.released[0].previous == struck.assignment
    successor = Assignment('other', 3.0, 'lead', 1.0, 'next')
    assert released.proposed_assignment(successor).previous_owners == (struck.assignment.owner_identity,)
    assert FieldCodec.decode(Todo, FieldCodec.encode(released)) == released


def test_order_fire_releases_every_open_problem_the_unit_holds(commander, commander_script, capsys, tmp_path):
    comms, _ = commander
    script, _ = commander_script
    create(commander, 'P1')
    create(commander, 'P2', 'printf 2', 2)
    script.FireVerb(unit='worker', why=FIRED).run()
    assert [(t.assignment, t.released[0].reason.text) for t in map(comms.goals.todos.get, ('P1', 'P2'))] == [(None, FIRED)] * 2
    assert capsys.readouterr().out.splitlines() == [f'{pid}: released from worker: {FIRED}' for pid in ('P1', 'P2')]
    script.board()
    assert (tmp_path / 'board.md').read_text().count(f'unassigned: released from worker: {FIRED}') == 2


@pytest.mark.parametrize('step, name, column', [
    ('carry_todo_voids', 'TodoVoids', 'voided'), ('carry_todo_releases', 'TodoReleases', 'released')])
def test_todo_record_carries_keep_every_stored_strike(commander, step, name, column):
    """A store from before the column gains it empty once; its rows, strikes and schema then read as declared."""
    import sqlite3
    from contextlib import closing
    from agent_comms.todos import TodoStore
    carry = getattr(importlib.import_module(f'cutover.{step}'), name)
    comms, caller = commander
    todo = create(commander, 'P1')
    CommsTodoTool(id='P1', operation=StrikeTodo(todo.revision, comms.registry.require('lead').incarnation,
        todo.assignment, 'The unit delivered its order after the recorded deadline had already passed.')).apply(comms, caller)
    struck = comms.goals.todos.get('P1')
    path = TodoStore.path_in(comms.root)
    with closing(sqlite3.connect(path)) as db:
        db.execute(f'ALTER TABLE todo DROP COLUMN {column}')
        db.commit()
    with pytest.raises(Exception):
        comms.goals.todos.get('P1')
    assert carry.carry(comms.root, apply=False).changed == 1
    assert carry.carry(comms.root, apply=True).changed == 1
    assert carry.carry(comms.root, apply=False).changed == 0
    assert comms.goals.todos.get('P1') == struck


def test_an_open_request_alarms_until_an_order_serves_it_and_closes_only_once_none_is_open(
        commander, commander_script):
    """The owner request is the commander's Goal: rotation names it while no open order serves it,
    each order stores a GoalRef to it, and it cannot be marked done or replaced while one is open."""
    import importlib
    from agent_comms.commander import request
    comms, caller = commander
    request = importlib.reload(request)
    assert request.stalled() == [
        'REQUEST STALLED: request Owner request -> order its next step or mark it done']
    todo = create(commander, 'P1')
    lead = comms.registry.require('lead').incarnation
    assert todo.goal == GoalRef('lead', lead.created_at, 'request')
    assert request.stalled() == []
    with pytest.raises(ValueError, match='still has open orders'):
        request.RequestCompleteVerb(id='request', why='The owner request is finished').run()
    with pytest.raises(ValueError, match='still open'):
        request.RequestAddVerb(text='A second owner request while the first is open').run()
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery()).apply(comms, caller)
    assert request.stalled() == [
        'REQUEST STALLED: request Owner request -> order its next step or mark it done']
    request.RequestCompleteVerb(id='request', why='The owner request is finished').run()
    assert request.stalled() == []
