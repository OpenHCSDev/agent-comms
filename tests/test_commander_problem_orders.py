"""One unit Goal reads its independent canonical Todo orders through real tools."""
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from contextlib import closing
from dataclasses import replace

from cutover.carry import CarryChain
from cutover.carry_todo_verdicts import TodoVerdicts

from agent_comms.change_report import FileLine
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.goal_actions import CreateProblemGoalAction, OrderGoalAction, SetGoalAction
from agent_comms.goal_states import CompletedGoal
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import (CommsCorrectWordingTool, CommsFireTool, CommsOrderCheckTool, CommsOrderDoneTool, CommsOrderTool,
    CommsOrderWorkTool, CommsProblemCloseTool, CommsTodoCreateTool, CommsTodoListTool, CommsTodoTool, CommsVerdictTool)
from agent_comms.todos import (AwaitingFirstCall, OrderDeletion, GoalRef, CleanHunt, CommitDelivery, FirstCall, Hunt, OrderDelivery,
    ApplicationOrderRun, RecordedCommitDelivery, MechanicalOrderRun, TodoStore,
    LateObservedFirstCall, OperationDelivery, OrderedHunt,
    NotifiedFirstCallEvent, ObservedFirstCallEvent, PendingProblemOrder, RecordFirstCallTodo,
    AcceptedVerdict, Assignment, AssignTodo, DoneTodoState, RecordHuntTodo, ReleaseTodo, SendBackVerdict, StrikeReason,
    StrikeTodo, Todo, TodoConflict, TodoError, VoidStrikeTodo)


def order(root, identity, command='printf 1', expected=1):
    return PendingProblemOrder(id=identity, owner=FileLine(__file__, 1), run=MechanicalOrderRun(),
        check=OrderCheck(str(root), command, CountCheckResult(expected)),
        edits=('Repair the existing owner',), banned=('No second store',), report='Report the result',
        at=RecordedEventTime(100), first_call_deadline=RecordedEventTime(220))


def create(commander, problem, command='printf 1', expected=1, deadline=1000, owner='worker'):
    comms, caller = commander
    CommsTodoCreateTool(owner=owner, order=CreateProblemGoalAction(
        problem=problem, order=order(comms.root, 'order-' + problem, command, expected),
        request='request', deadline=RecordedEventTime(deadline), commander='lead',
        repo='OpenHCSDev/agent-comms', text='Fix ' + problem,
    )).apply(comms, caller)
    return comms.goals.todos.get(problem)


def test_order_work_reads_the_assignees_calls_since_its_order_newest_first(commander, tmp_path):
    comms, caller = commander
    session = tmp_path / 'unit.jsonl'

    def turn(n, at, *calls):
        stamp = f'1970-01-01T00:{at // 60:02d}:{at % 60:02d}+00:00'
        yield dict(type='message', id=f'u{n}', timestamp=stamp, message=dict(role='user', content='go'))
        yield dict(type='message', id=f'a{n}', timestamp=stamp, message=dict(role='assistant', content=[
            dict(type='toolCall', id=call, name='read', arguments=dict(path=f'{call}.py')) for call in calls],
            stopReason='toolUse'))
        for call in calls[:-1]:
            yield dict(type='message', id=f'r{call}', timestamp=stamp, message=dict(
                role='toolResult', toolCallId=call, toolName='read', isError=call == 'late',
                content=[dict(type='text', text='contents')]))

    rows = [dict(type='session', id='source', version=3), *turn(1, 50, 'before'),
            *turn(2, 150, 'early', 'late', 'running')]
    session.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    session.chmod(0o600)
    comms.registry.declare(comms.registry.new_thread(
        'unit', frozenset(), str(tmp_path), session_file=str(session), created_at=3.0))
    comms.goals.update_goal('unit', SetGoalAction(text='Unit brief', goal_id='unit-goal'))
    create(commander, 'P1', owner='unit')
    work = CommsOrderWorkTool(problem='P1').apply(comms, caller)
    assert work.thread.name == 'unit' and work.since == 100
    assert [(call.tool_call_id, call.digest, call.state.declared_name) for call in work.calls] == [
        ('running', 'read running.py', 'running'), ('late', 'read late.py', 'failed'),
        ('early', 'read early.py', 'succeeded')]


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


def test_delivered_order_is_not_transferred(commander, tmp_path):
    comms, caller = commander

    def git(*args):
        return subprocess.check_output(('git', '-C', str(comms.root), '-c', 'user.name=t',
            '-c', 'user.email=t@t', *args), stderr=subprocess.DEVNULL).decode().strip()

    git('init', '-q', '-b', 'main')
    git('commit', '-q', '--allow-empty', '-m', 'Delivered fix')
    commit = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/fix', commit)
    marker = comms.root / 'check-calls'
    create(commander, 'P1', f"printf x >> '{marker}'; printf 1")
    create(commander, 'P2')
    CommsOrderDoneTool(problem='P1', delivery=CommitDelivery(
        commit=commit, delivered_commit=commit)).apply(comms, caller)
    assert marker.read_text() == 'x'

    copied = Comms(shutil.copytree(comms.root, tmp_path / 'copied-root'))
    delivered = copied.goals.todos.get('P1')
    pending = copied.goals.todos.get('P2')
    successor = Assignment('other', 3.0, 'lead', 1.0, 'next')

    def transfer(todo):
        return copied.goals.todos.apply(todo.id, AssignTodo(todo.revision, successor,
            RecordedEventTime(3000), order(copied.root, 'replacement-' + todo.id), todo.goal))

    with pytest.raises(ValueError, match=f'worker already has delivered commit {commit}; worker merges it'):
        transfer(delivered)
    assert copied.goals.todos.get('P1') == delivered
    assert delivered.order.delivery == comms.goals.todos.get('P1').order.delivery
    transferred = transfer(pending)
    assert transferred.assignment.owner == 'other'
    assert transferred.assignment.previous_owners == (pending.assignment.owner_identity,)
    assert copied.goals.todos.get('P2') == transferred
    released = copied.goals.todos.apply('P1', ReleaseTodo(delivered.revision,
        copied.registry.require('lead').incarnation, delivered.assignment, StrikeReason(FIRED)))
    with pytest.raises(ValueError, match=f'worker already has delivered commit {commit}; worker merges it'):
        transfer(released)
    assert copied.goals.todos.get('P1') == released
    assert marker.read_text() == (copied.root / 'check-calls').read_text() == 'x'


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


def test_a_main_merge_is_never_the_delivered_commit(commander):
    """P44 recorded the commander's main merge as the delivery, 72 minutes after the unit's own push."""
    comms, caller = commander
    def git(*args):
        return subprocess.check_output(('git', '-C', str(comms.root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                                        *args), stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q', '-b', 'main')
    git('commit', '-q', '--allow-empty', '-m', 'base')
    git('checkout', '-q', '-b', 'work')
    git('commit', '-q', '--allow-empty', '-m', 'the answer')
    git('checkout', '-q', 'main')
    git('merge', '-q', '--no-ff', '-m', 'merged', 'work')
    merge = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', merge)
    create(commander, 'P1')
    with pytest.raises(ValueError, match='is a merge'):
        CommsOrderDoneTool(problem='P1', delivery=CommitDelivery(commit=merge, delivered_commit=merge)).apply(comms, caller)
    answer = git('rev-parse', 'work')
    CommsOrderDoneTool(problem='P1', delivery=CommitDelivery(commit=merge, delivered_commit=answer)).apply(comms, caller)
    assert not comms.goals.todos.get('P1').pending


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
    sent = judge.my_messages('worker')[0]['text']
    assert 'Set count.txt to 1.' in sent and 'Start now and push as you go.' in sent
    assert 'Due' not in sent and due not in sent  # the deadline stays the commander's fire line

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


def order_body(repo, check=None, edit='Set count.txt to 1.', extra=''):
    """An order body the commander script accepts, with its check and one numbered edit."""
    body = repo.parent / 'order.txt'
    body.write_text(f"Problem: NEW\nOwner: {__file__}:1\nCheck: {repo} :: {check or 'cat count.txt -> 1'}\n"
                    "Works when: the installed app shows the counter at one after restart\n"
                    f"The counter is wrong and that is stupid.\n1. {edit}\n{extra}"
                    "Banned: no second store; no cache\nReport: the commit and the count before -> after\n")
    return str(body)


def problem(script, body):
    script.problem('The counter file holds zero where the fleet needs one', 'worker', '23:59', body, 'request')


def test_resend_after_strike_counts_one_miss(commander, commander_script, monkeypatch):
    """A miss struck with `order strike` and then sent back is one strike; the next send-back strikes again."""
    comms, _ = commander
    script, repo = commander_script
    monkeypatch.setattr(script, 'post', lambda recipients, body: None)
    (repo / 'count.txt').write_text('0\n')
    body = order_body(repo)
    problem(script, body)
    script.strike('worker', 'P1 left count.txt at zero and reported it fixed')
    script.send('worker', '23:59', body, 'request')
    assert script.struck(comms.goals.todos.get('P1'), 'worker') == 1
    script.send('worker', '23:59', body, 'request')
    assert script.struck(comms.goals.todos.get('P1'), 'worker') == 2


def test_a_deletion_order_must_check_the_imports_of_every_repo_that_imports_the_module(
        commander, commander_script, monkeypatch, tmp_path):
    """P12 deleted acp_ingress.py while Toad imported it; the check must read Toad's imports too."""
    comms, _ = commander
    script, repo = commander_script
    monkeypatch.setattr(script, 'post', lambda recipients, body: None)
    toad = tmp_path / 'toad'
    (toad / 'src').mkdir(parents=True)
    (toad / 'src' / 'app.py').write_text('from agent_comms.acp_ingress import Ingress\n')
    monkeypatch.setattr(script, 'CONFIG', script.CONFIG.model_copy(update={'repos': (
        script.Repo(checkout=toad, github='OpenHCSDev/toad'),)}))
    (repo / 'acp_ingress.py').write_text('class Ingress: ...\n')
    edit = 'Delete src/agent_comms/acp_ingress.py.'
    with pytest.raises(SystemExit, match=rf"\['{toad}'\] import"):
        problem(script, order_body(repo, 'grep -rl Ingress . | wc -l -> 0', edit))
    problem(script, order_body(repo, f'grep -rl Ingress . {toad} | wc -l -> 0', edit))
    assert comms.goals.todos.get('P1').order.check.command == f'grep -rl Ingress . {toad} | wc -l'


def test_an_order_changing_tools_must_ratchet_both_roots(commander, commander_script, monkeypatch):
    comms, _ = commander
    script, repo = commander_script
    monkeypatch.setattr(script, 'post', lambda recipients, body: None)
    (repo / 'count.txt').write_text('0\n')
    edit = 'Count the measure in tools/debt_ratchet.py.'
    with pytest.raises(SystemExit, match='--root tools and with --root src'):
        problem(script, order_body(repo, edit=edit))
    problem(script, order_body(repo, edit=edit, extra='Ratchet with --root tools and with --root src, each as its own command.\n'))
    assert comms.goals.todos.get('P1').order.edits == (edit,)


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


def test_a_withdrawn_order_is_neither_open_nor_overdue_and_strikes_no_one(commander, commander_script, tmp_path,
                                                                           monkeypatch):
    comms, _ = commander
    script, _ = commander_script
    create(commander, 'P1', deadline=1)
    posted = []
    monkeypatch.setattr(script, 'post', lambda recipients, body: posted.append((recipients, body)))
    script.withdraw('P1', 'duplicate of P2, which owns the same fix', 'P2')
    withdrawn = comms.goals.todos.get('P1')
    assert script.open_todos() == [] and not withdrawn.pending and script.struck(withdrawn, 'worker') == 0
    line = 'P1: order order-P1 withdrawn from worker, no strike: duplicate of P2, which owns the same fix (superseded by P2)'
    assert (tmp_path / 'board.md').read_text().count(line) == 1
    assert line in (tmp_path / 'narrative.md').read_text()
    assert [recipients for recipients, _ in posted] == [['#comms'], ['worker']]
    with pytest.raises(ValueError, match='Only an undelivered order'):
        script.withdraw('P1', 'withdrawing the same order twice records nothing new')


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


def test_one_fire_call_releases_both_open_orders_with_its_reason(commander):
    comms, caller = commander
    held = (create(commander, 'P1'), create(commander, 'P2', 'printf 2', 2))
    with pytest.raises(TodoError, match='holds no open problem'):
        CommsFireTool(unit='worker', reason=StrikeReason(FIRED)).apply(comms, ThreadCaller('worker'))
    fired = CommsFireTool(unit='worker', reason=StrikeReason(FIRED)).apply(comms, caller).todos
    assert fired == tuple(map(comms.goals.todos.get, ('P1', 'P2')))
    assert [(t.assignment, t.revision, t.released[0].previous, t.released[0].reason.text) for t in fired] == [
        (None, before.revision + 1, before.assignment, FIRED) for before in held]


def test_unit_wording_gets_a_correction_not_a_strike(commander):
    comms, caller = commander
    held = create(commander, 'P1')
    with pytest.raises(ValueError, match="unit's wording; send it a correction"):
        StrikeTodo(held.revision, comms.registry.require('lead').incarnation, held.assignment,
                   'The unit wrote "canonical" and "ownership" in its report to the commander again.'
                   ).apply(held)
    with pytest.raises(ValueError, match='nothing to correct'):
        CommsCorrectWordingTool(unit='worker', message_id='m0', text='The tests pass now.').apply(comms, caller)
    CommsCorrectWordingTool(unit='worker', message_id='m1',
                            text='The canonical ownership changed').apply(comms, caller)
    assert comms.bus.inbox('worker')[-1].body.startswith(
        'Correction, no strike, on P1: message m1 used canonical, ownership.')
    assert comms.goals.todos.get('P1') == held and held.strike_count == 0


def test_published_unit_jargon_is_corrected_on_its_open_order(commander):
    """No watch runs: publishing the message is what records the correction on the order."""
    comms, _ = commander
    create(commander, 'P1')
    vague = comms.messaging.send('worker', 'lead', 'The canonical ownership changed')
    comms.messaging.send('worker', 'lead', 'The tests pass now.')
    comms.messaging.send('lead', 'worker', 'Your lifecycle report is late.')
    corrected = comms.goals.todos.get('P1')
    assert [(c.message_id, c.words) for c in corrected.corrections] == [(vague, ('canonical', 'ownership'))]
    assert corrected.strike_count == 0


@pytest.mark.parametrize('step, name, column', [
    ('carry_todo_voids', 'TodoVoids', 'voided'), ('carry_todo_releases', 'TodoReleases', 'released'),
    ('carry_todo_verdicts', 'TodoVerdicts', 'verdicts'),
    ('carry_todo_corrections', 'TodoCorrections', 'corrections')])
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


def stray(commander, problem, generation):
    """A pending problem stored the way pre-P48 joins and refused sends left them: under a Goal id that is
    not the unit's open Goal."""
    comms, _ = commander
    lead = comms.registry.require('lead')
    return comms.goals.todos.create(problem, 'OpenHCSDev/agent-comms', 'Fix ' + problem, creator=lead,
        owner=comms.registry.require('worker'), generation=generation, deadline=RecordedEventTime(1000),
        order=order(comms.root, 'order-' + problem), goal=GoalRef('lead', lead.created_at, 'request'))


def completed_with_pending(commander):
    """ci-1370's shape: the Goal completed on its only problem, then later problems were stored under it
    and under a Goal that was never published."""
    comms, caller = commander
    create(commander, 'P1')
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery()).apply(comms, caller)
    goal = comms.registry.require('worker').goal
    assert isinstance(goal.state, CompletedGoal)
    stray(commander, 'P2', goal.id)
    stray(commander, 'P3', 'never-published')
    return goal


def todo_row(comms, problem):
    with closing(sqlite3.connect(TodoStore.path_in(comms.root))) as db:
        return db.execute('SELECT * FROM todo WHERE id=?', (problem,)).fetchone()


def test_goal_stays_open_while_problems_pend(commander):
    """Delivering a problem never completes the Goal while another assigned problem pends, even one
    stored under a stale generation; a revised order for one keeps the other's order and deadline."""
    comms, caller = commander
    create(commander, 'P1')
    create(commander, 'P2', deadline=2000)
    stray(commander, 'P3', 'never-published')
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery()).apply(comms, caller)
    assert comms.registry.require('worker').goal.state.active
    other = comms.goals.todos.get('P3')
    CommsOrderTool(owner='worker', order=OrderGoalAction(problem='P2', order=order(comms.root, 'revised'),
        request='request', deadline=RecordedEventTime(3000), commander='lead',
        expected_revision=comms.goals.todos.get('P2').revision)).apply(comms, caller)
    assert comms.goals.todos.get('P2').order.id == 'revised'
    assert comms.goals.todos.get('P3') == other
    CommsOrderDoneTool(problem='P2', delivery=OperationDelivery()).apply(comms, caller)
    assert comms.registry.require('worker').goal.state.active


def test_a_refused_send_leaves_the_todo_row_byte_identical(commander):
    comms, caller = commander
    goal = completed_with_pending(commander)
    before = todo_row(comms, 'P3')
    with pytest.raises(ValueError, match='Finish or release the assigned problems'):
        CommsOrderTool(owner='worker', order=OrderGoalAction(problem='P3', order=order(comms.root, 'resent'),
            request='request', deadline=RecordedEventTime(3000), commander='lead',
            expected_revision=comms.goals.todos.get('P3').revision)).apply(comms, caller)
    assert todo_row(comms, 'P3') == before
    assert comms.registry.require('worker').goal == goal


def test_column_carry_then_goal_carry_reads_todos(commander, tmp_path):
    """The deleted goal carry runs before the verdicts column carry, from the revision that added
    it, whose Todo reads a pre-verdicts store; the current Todo reports that column missing."""
    comms, _ = commander
    completed_with_pending(commander)
    path = TodoStore.path_in(comms.root)
    with closing(sqlite3.connect(path)) as db:
        db.execute('ALTER TABLE todo DROP COLUMN verdicts')
        db.commit()
    with (closing(CoordinationStore.read_snapshot(path)) as db,
          pytest.raises(sqlite3.OperationalError, match='no such column: "verdicts"')):
        list(Todo.stream(db))
    repo = Path(__file__).parents[1]
    steps = {step.name: step for step in CarryChain.at(repo, 'HEAD').steps}
    assert list(steps).index('carry_todo_verdicts') > list(steps).index('carry_problem_goals')
    held = tmp_path / 'held'
    held.mkdir()
    archive = subprocess.run(['git', '-C', str(repo), 'archive', steps['carry_problem_goals'].revision,
                              'src', 'tools'], capture_output=True, check=True).stdout
    subprocess.run(['tar', '-x', '-C', str(held)], input=archive, check=True)
    ran = subprocess.run([sys.executable, '-m', 'cutover.carry_problem_goals', str(comms.root)],
                         capture_output=True, text=True, cwd=held,
                         env={**os.environ, 'PYTHONPATH': f'{held / "src"}:{held / "tools"}'})
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout.splitlines()[-1])['changed'] == 2
    assert TodoVerdicts.carry(comms.root, apply=True).changed == 3
    with closing(CoordinationStore.read_snapshot(path)) as db:
        assert {todo.id for todo in Todo.stream(db)} == {'P1', 'P2', 'P3'}


def test_order_run_carry_gives_a_pre_p511_order_its_declared_run(commander):
    """An order saved before closing runs is refused until the carry gives it the mechanical run it implied."""
    import json
    import sqlite3
    from contextlib import closing
    from cutover.carry_order_run import OrderRuns
    from agent_comms.todos import MechanicalOrderRun, TodoStore
    comms, _caller = commander
    saved = create(commander, 'P1')
    path = TodoStore.path_in(comms.root)
    with closing(sqlite3.connect(path)) as db:
        (order,), = db.execute('SELECT "order" FROM todo WHERE id = ?', ('P1',))
        db.execute('UPDATE todo SET "order" = ? WHERE id = ?',
                   (json.dumps({k: v for k, v in json.loads(order).items() if k != 'run'}), 'P1'))
        db.commit()
    with pytest.raises(MalformedRecord, match='run'):
        comms.goals.todos.get('P1')
    assert OrderRuns.carry(comms.root, apply=False).detail == {MechanicalOrderRun.declared_name: 1}
    assert OrderRuns.carry(comms.root, apply=True).changed == 1
    assert OrderRuns.carry(comms.root, apply=False).changed == 0
    assert comms.goals.todos.get('P1') == saved


def test_a_rotation_pass_names_the_unit_with_orders_that_reads_nothing(commander, commander_script, tmp_path):
    """Two CLI units hold orders; only the one whose bridge left is detached, with its unread count."""
    import importlib
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.thread_execution import ExternalThreadExecution
    from agent_comms.thread_status import StoppedThreadStatus

    comms, _ = commander
    for created, unit in enumerate(('parted', 'watching'), 3):
        comms.registry.declare(comms.registry.new_thread(
            unit, frozenset(), str(tmp_path), created_at=float(created), execution=ExternalThreadExecution),
            StoppedThreadStatus())
        comms.goals.update_goal(unit, SetGoalAction(text='Commander brief', goal_id=f'{unit}-brief'))
    create(commander, 'P1', owner='parted')
    create(commander, 'P2', owner='watching')
    comms.messaging.send('lead', 'parted', 'Are you there?')
    rotate = importlib.reload(importlib.import_module('agent_comms.commander.rotate'))
    with comms.registry.external_process('watching', ProcessIdentity.capture(os.getpid())):
        lines = rotate.detached(rotate.owed_orders())
    assert lines == ['DETACHED parted: 1 unread, orders P1 -> wake it or reassign']


def test_an_accepted_verdict_quotes_the_order_it_serves(commander):
    """An accepted verdict stays on the order only when its quote is 4+ words of that order."""
    comms, caller = commander
    todo = create(commander, 'P1')
    lead = comms.registry.require('lead').incarnation
    with pytest.raises(ValueError, match='Quote 4\\+ words'):
        CommsVerdictTool(id='P1', operation=AcceptedVerdict(
            todo.revision, lead, todo.assignment, 'toolu_1', quote='Delete the whole bus now')).apply(comms, caller)
    assert comms.goals.todos.get('P1') == todo
    accepted = AcceptedVerdict(todo.revision, lead, todo.assignment, 'toolu_1', quote='Repair  the existing owner')
    CommsVerdictTool(id='P1', operation=accepted).apply(comms, caller)
    judged = comms.goals.todos.get('P1')
    assert (judged.verdicts, judged.strikes, judged.revision) == ((accepted,), (), todo.revision + 1)


def test_a_send_back_strikes_the_holder_and_revises_its_goal_in_one_revision(commander):
    """A send-back is one verdict and one strike on the order, and the holder's Goal says why."""
    comms, caller = commander
    todo = create(commander, 'P1')
    goal = comms.registry.require('worker').goal
    why = 'The unit wrapped the old store in a forwarding class instead of deleting it.'
    sent_back = SendBackVerdict(todo.revision, comms.registry.require('lead').incarnation, todo.assignment,
                                'toolu_2', reason=StrikeReason(why))
    CommsVerdictTool(id='P1', operation=sent_back).apply(comms, caller)
    judged = comms.goals.todos.get('P1')
    assert (judged.verdicts, judged.revision, judged.strike_count) == ((sent_back,), todo.revision + 1, 1)
    assert judged.strikes[0].reason == why
    revised = comms.registry.require('worker').goal
    assert (revised.id, revised.revision, revised.progress) == (goal.id, goal.revision + 1, f'Sent back on P1: {why}')


def test_close_quotes_the_works_line(commander):
    """Closing evidence must quote four consecutive words of the order's Works when run."""
    comms, caller = commander
    run = ApplicationOrderRun('The board shows the problem closed after the commander reopens the session')
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem='P1', order=replace(order(comms.root, 'order-P1'), run=run), request='request',
        deadline=RecordedEventTime(1000), commander='lead', repo='OpenHCSDev/agent-comms', text='Fix P1',
    )).apply(comms, caller)
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery()).apply(comms, caller)
    artifact = comms.root / 'installed-evidence'
    artifact.write_text('Verified installed path')
    delivered = comms.goals.todos.get('P1')
    with pytest.raises(ValueError, match='consecutive words of: Works when:'):
        CommsProblemCloseTool(problem='P1', artifact=str(artifact),
            works='The installed application kept both queued orders through tab returns.').apply(comms, caller)
    assert comms.goals.todos.get('P1') == delivered
    CommsProblemCloseTool(problem='P1', artifact=str(artifact),
        works='In the installed app the board shows the problem closed, read after a restart.').apply(comms, caller)
    assert comms.goals.todos.get('P1').state.declared_name == 'done'


def test_delivery_that_renames_the_decision_is_refused(commander, tmp_path):
    comms, caller = commander
    copied = tmp_path / 'copied'
    copied.mkdir()
    (copied / 'flag.py').write_text('def is_set(value):\n    return value is not None\n')
    callers = copied / 'callers.py'
    callers.write_text('from flag import is_set\n\ndef read(v):\n    return 1 if is_set(v) else 0\n')
    family = ("grep -rhoE 'is_set\\(|except ValueError' --include=*.py . | wc -l")
    deletes = OrderDeletion.parse(f'is_set absence decision at {copied / "flag.py"}:1 :: {copied} :: {family}')
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem='P520', order=replace(order(comms.root, 'order-P520'), deletes=deletes),
        request='request', deadline=RecordedEventTime(1000), commander='lead',
        repo='OpenHCSDev/agent-comms', text='Delete the is_set decision',
    )).apply(comms, caller)
    pending = comms.goals.todos.get('P520')
    assert 'Deletes: is_set absence decision at' in pending.order.instructions(pending)
    assert FieldCodec.decode(Todo, FieldCodec.encode(pending)) == pending
    (copied / 'flag.py').write_text('def checked(value):\n    if value is None:\n        raise ValueError\n')
    callers.write_text('from flag import checked\n\ndef read(v):\n    try:\n        checked(v)\n'
                       '    except ValueError:\n        return 0\n    return 1\n')
    with pytest.raises(ValueError, match='expected last integer 0'):
        CommsOrderDoneTool(problem='P520', delivery=OperationDelivery()).apply(comms, caller)
    assert comms.goals.todos.get('P520') == pending
    (copied / 'flag.py').unlink()
    callers.write_text('def read(v):\n    return int(v is not None)\n')
    CommsOrderDoneTool(problem='P520', delivery=OperationDelivery()).apply(comms, caller)
    assert not comms.goals.todos.get('P520').pending
