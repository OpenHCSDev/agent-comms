"""One unit Goal reads its independent canonical Todo orders through real tools."""
import asyncio
import importlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from contextlib import closing
from dataclasses import replace

from cutover.carry import CarryChain

from agent_comms.change_report import FileLine
from agent_comms.channel_targets import CommandTarget
from agent_comms.child_process import BoundedRun
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.goal_actions import BlockedGoalAction, CreateProblemGoalAction, ModelInvocable, OrderGoalAction, SetGoalAction
from agent_comms.goal_states import CompletedGoal
from agent_comms.goals import CountCheckResult, OrderCheck, ProblemGoalOrder
from agent_comms.progress_claims import WorkingClaim
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import (CommsCorrectWordingTool, CommsSendTool, CommsFireTool, CommsOrderCheckTool, CommsOrderDoneTool, CommsOrderTool,
    CommsOrderWorkTool, CommsProblemCloseTool, CommsTodoCreateTool, CommsTodoListTool, CommsTodoTool, CommsVerdictTool)
from agent_comms.todos import (AwaitingFirstCall, VerdictAudience, OrderMessage, OrderDeletion, GoalRef, CleanHunt, CommitDelivery, FirstCall, Hunt, OrderDelivery,
    ApplicationOrderRun, RecordedCommitDelivery, MechanicalOrderRun, TodoStore,
    LateObservedFirstCall, OperationDelivery, OrderedHunt,
    NotifiedFirstCallEvent, ObservedFirstCallEvent, PendingProblemOrder, RecordFirstCallTodo,
    AcceptedVerdict, Assignment, AssignTodo, DoneTodoState, RecordHuntTodo, ReleaseTodo, SendBackVerdict, StrikeReason,
    StrikeTodo, Todo, TodoConflict, TodoError, VoidStrikeTodo)
from agent_comms.turn_context import GoalSegment


def order(root, identity, command='printf 1', expected=1):
    return PendingProblemOrder(id=identity, owner=FileLine(__file__, 1), run=MechanicalOrderRun(),
        check=OrderCheck(str(root), command, CountCheckResult(expected)),
        edits=('Repair the existing owner',), banned=('No second store',), report='Report the result',
        at=RecordedEventTime(100), first_call_deadline=RecordedEventTime(220))


def create(commander, problem, command='printf 1', expected=1, deadline=1000, owner='worker', directory=None):
    comms, caller = commander
    CommsTodoCreateTool(owner=owner, order=CreateProblemGoalAction(
        problem=problem, order=order(directory or comms.root, 'order-' + problem, command, expected),
        request='request', deadline=RecordedEventTime(deadline), commander='lead',
        repo='OpenHCSDev/agent-comms', text='Fix ' + problem,
    )).apply(comms, caller)
    return comms.goals.todos.get(problem)


def working(comms):
    """The working claim a delivery stands on: a screenshot that exists."""
    screenshot = Path(comms.root) / 'working.png'
    screenshot.write_bytes(b'png')
    return (WorkingClaim(str(screenshot)),)


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
    assert (goal.id, goal.text, goal.progress, goal.state) == (original.id, ProblemGoalOrder.text, '', original.state)
    assert first.assignment.generation == second.assignment.generation == goal.id
    assert first.deadline == RecordedEventTime(1000)
    assert second.deadline == RecordedEventTime(2000)
    assert first.order.check.result == CountCheckResult(1)
    assert second.order.check.result == CountCheckResult(2)
    assert first.order.first_call_deadline == RecordedEventTime(220)
    rendered = '\n'.join(goal.instructions_for(comms.goals, comms.registry.require('worker').incarnation))
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
    assert comms.registry.require('worker').goal.text == ProblemGoalOrder.text
    worker = comms.registry.require('worker').incarnation
    rows, rendered = comms.goals.todos.for_goal(worker, goal.id), '\n'.join(goal.instructions_for(comms.goals, worker))
    assert 'printf 7' in rendered and 'printf 1' not in rendered
    assert rows[0].deadline == RecordedEventTime(3000)


def test_strike_time_and_clean_ordered_hunts_belong_to_problem(commander):
    comms, caller = commander
    todo = create(commander, 'P1')
    actor = comms.registry.require('lead').incarnation
    CommsTodoTool(id='P1', operation=StrikeTodo(todo.revision, actor, todo.assignment,
        f'The assigned change broke the existing resource release path in {__file__} during review.', RecordedEventTime(500))).apply(comms, caller)
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
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert marker.read_text() == 'x'
    assert comms.registry.require('worker').goal.state.active
    assert not comms.goals.todos.get('P1').pending
    assert comms.goals.todos.get('P1').order.delivery.at.at > 0
    goal = comms.registry.require('worker').goal
    rendered = '\n'.join(goal.instructions_for(comms.goals, comms.registry.require('worker').incarnation))
    assert 'Problem: P1' not in rendered and 'Problem: P2' in rendered
    CommsOrderDoneTool(problem='P2', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert isinstance(comms.registry.require('worker').goal.state, CompletedGoal)


def test_failed_check_keeps_problem_pending(commander):
    comms, caller = commander
    original = create(commander, 'P1', 'printf 9', 1)
    with pytest.raises(ValueError, match='printed 9, not its expected count 1'):
        CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert comms.goals.todos.get('P1') == original
    assert comms.registry.require('worker').goal.state.active


def test_close_records_evidence_once_and_does_not_depend_on_artifact_lifetime(commander):
    comms, caller = commander
    create(commander, 'P1')
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
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
    CommsOrderDoneTool(problem='P1', delivery=CommitDelivery(commit=commit, delivered_commit=commit), claims=working(comms)).apply(comms, caller)
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
        commit=commit, delivered_commit=commit), claims=working(comms)).apply(comms, caller)
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
        CommsOrderDoneTool(problem='P1', delivery=CommitDelivery(commit=merge, delivered_commit=delivered), claims=working(comms)).apply(comms, caller)
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
        CommsOrderDoneTool(problem='P2', delivery=CommitDelivery(commit=late, delivered_commit=side), claims=working(comms)).apply(comms, caller)
    script.done('P2', late, late)
    struck = comms.goals.todos.get('P2')
    assert script.struck(struck, 'worker') == 1 and late in struck.strikes[-1].reason


def test_a_late_delivery_reads_its_lateness_to_the_second(commander, commander_script):
    """P136 was due 21:20:00 and delivered 21:20:35; its strike read "due 21:20 delivered 21:20"."""
    comms, _ = commander
    script, _ = commander_script

    def git(*args, at=None):
        env = {**os.environ, 'GIT_AUTHOR_DATE': f'@{at} +0000', 'GIT_COMMITTER_DATE': f'@{at} +0000'} if at else None
        return subprocess.check_output(('git', '-C', str(comms.root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                                        *args), env=env, stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q', '-b', 'main')
    git('commit', '-q', '--allow-empty', '-m', 'base', at=500)
    clock = lambda at: time.strftime('%H:%M:%S', time.localtime(at))  # noqa: E731
    for pid, at in (('P1', 2000), ('P2', 2035)):
        create(commander, pid, deadline=2000)
        git('commit', '-q', '--allow-empty', '-m', f'{pid} delivered', at=at)
        delivered = git('rev-parse', 'HEAD')
        git('update-ref', 'refs/remotes/origin/main', delivered)
        script.done(pid, delivered, delivered)
    assert script.struck(comms.goals.todos.get('P1'), 'worker') == 0
    (strike,) = comms.goals.todos.get('P2').strikes
    assert strike.reason.startswith(f'P2 order order-P2 late by 35 s: due {clock(2000)}, '
                                    f'delivered {clock(2035)} as {delivered}')


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
        CommsOrderDoneTool(problem='P1', delivery=CommitDelivery(commit=merge, delivered_commit=merge), claims=working(comms)).apply(comms, caller)
    answer = git('rev-parse', 'work')
    CommsOrderDoneTool(problem='P1', delivery=CommitDelivery(commit=merge, delivered_commit=answer), claims=working(comms)).apply(comms, caller)
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
                    f"The counter in {repo / 'count.txt'} is wrong and that is stupid.\n1. Set count.txt to 1.\n"
                    "Banned: no second store; no cache\nReport: the commit and the count before -> after\n")
    due = '23:59'
    script.problem('The counter file holds zero where the fleet needs one', 'worker', due, str(body), 'request')
    created = comms.goals.todos.get('P1')
    assert (created.assignment.owner, created.order.edits, created.order.banned) == (
        'worker', ('Set count.txt to 1.',), ('no second store', 'no cache'))

    script.send('worker', due, str(body), 'request')  # the same holder again without --clean is a send-back
    sent_back = comms.goals.todos.get('P1')
    assert sent_back.order.id != created.order.id and script.struck(sent_back, 'worker') == 1
    (link,) = sent_back.messages[-1:]
    sent = next(m.body for m in comms.views.history_page(CommandTarget.named('worker'), viewer='lead').messages if m.message_id == link.message_id)
    assert 'Set count.txt to 1.' in sent and 'Start now and push as you go.' in sent
    assert 'Due' not in sent and due not in sent  # the deadline stays the commander's fire line

    script.hunt(f'{__file__}:1', 'P1', 'the counter is reread per event and the order replaces it with one read', sent_back.order.id)
    assert [h.sent for h in comms.goals.todos.get('P1').hunts] == [sent_back.order.id]

    (repo / 'count.txt').write_text('1\n')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'fix')
    git('update-ref', 'refs/remotes/origin/main', 'HEAD')
    script.done('P1', git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD'))
    assert not comms.goals.todos.get('P1').pending

    artifact = repo.parent / 'seen.png'
    artifact.write_text('x')
    script.close('P1', 'the installed app shows the counter at one after restart', str(artifact))
    assert not comms.goals.todos.get('P1').state.open and script.held_orders() == ()
    assert not {'problems.json', 'deadlines.json', 'closed.jsonl', 'strikes.jsonl', 'done.jsonl',
                'sent.jsonl', 'hunts.jsonl'} & {
        path.name for path in (repo.parent / 'state').iterdir()}


def order_body(repo, check=None, edit='Set count.txt to 1.', extra=''):
    """An order body the commander script accepts, with its check and one numbered edit."""
    body = repo.parent / 'order.txt'
    body.write_text(f"Problem: NEW\nOwner: {__file__}:1\nCheck: {repo} :: {check or 'cat count.txt -> 1'}\n"
                    "Works when: the installed app shows the counter at one after restart\n"
                    f"The counter in {repo / 'count.txt'} is wrong and that is stupid.\n1. {edit}\n{extra}"
                    "Banned: no second store; no cache\nReport: the commit and the count before -> after\n")
    return str(body)


def problem(script, body):
    script.problem('The counter file holds zero where the fleet needs one', 'worker', '23:59', body, 'request')


def test_resend_after_strike_counts_one_miss(commander, commander_script, monkeypatch):
    """A miss struck with `order strike` and then sent back is one strike; the next send-back strikes again."""
    comms, _ = commander
    script, repo = commander_script
    monkeypatch.setattr(script, 'post', lambda recipients, body: 'sent')
    (repo / 'count.txt').write_text('0\n')
    body = order_body(repo)
    problem(script, body)
    script.strike('worker', 'P1', f'P1 left {repo / "count.txt"} at zero and reported it fixed')
    script.send('worker', '23:59', body, 'request')
    assert script.struck(comms.goals.todos.get('P1'), 'worker') == 1
    script.send('worker', '23:59', body, 'request')
    assert script.struck(comms.goals.todos.get('P1'), 'worker') == 2


def test_a_strike_and_a_clean_hunt_land_on_their_problem_argument_not_one_the_why_cites(
        commander, commander_script, monkeypatch):
    comms, _ = commander
    script, repo = commander_script
    monkeypatch.setattr(script, 'post', lambda recipients, body: 'sent')
    (repo / 'count.txt').write_text('0\n')
    problem(script, order_body(repo))
    create(commander, 'P2', owner='worker', deadline=time.time() + 3600)
    script.strike('worker', 'P2', f'P1 was compared with this miss: P2 left {repo / "count.txt"} at zero and reported it fixed')
    assert [script.struck(comms.goals.todos.get(pid), 'worker') for pid in ('P1', 'P2')] == [0, 1]
    script.hunt(f'{__file__}:1', 'P2', 'P1 rereads the counter per event; this read of the P2 path found it clean', None)
    assert [len(comms.goals.todos.get(pid).hunts) for pid in ('P1', 'P2')] == [0, 1]


def test_a_deletion_order_must_check_the_imports_of_every_repo_that_imports_the_module(
        commander, commander_script, monkeypatch, tmp_path):
    """P12 deleted acp_ingress.py while Toad imported it; the check must read Toad's imports too."""
    comms, _ = commander
    script, repo = commander_script
    monkeypatch.setattr(script, 'post', lambda recipients, body: 'sent')
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
    monkeypatch.setattr(script, 'post', lambda recipients, body: 'sent')
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
    script.strike('worker', 'P1', f'P1 was sent back once before its check in {repo} was corrected')
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
    with pytest.raises(ValueError, match='printed 0, not its expected count 1'):
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
    assert script.held_orders() == () and 'Problem: NEW' in body.read_text()


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
    assert script.held_orders() == ()


@pytest.mark.parametrize('printed, refused', [
    ("printf 'no count here'", 'The order check printed no integer, so no count of 1.'),
    ('echo 2', 'The order check printed 2, not its expected count 1.'),
    ('echo found 1', None),
])
def test_a_count_check_refuses_saying_whether_it_printed_no_integer_or_the_wrong_count(tmp_path, printed, refused):
    ran = asyncio.run(BoundedRun.run(('bash', '-c', printed), cwd=str(tmp_path), timeout=30))
    if refused is None:
        CountCheckResult(1).require(ran)
    else:
        with pytest.raises(ValueError, match=re.escape(refused)):
            CountCheckResult(1).require(ran)


def test_a_voided_strike_stays_readable_stops_counting_and_voids_once(commander):
    """Only the commander voids; a void needs a live strike on this todo; a closed todo is voided alike."""
    comms, caller = commander
    todo = create(commander, 'P1')
    lead = comms.registry.require('lead').incarnation
    CommsTodoTool(id='P1', operation=StrikeTodo(todo.revision, lead, todo.assignment,
        f'The unit delivered {__file__} after the recorded deadline had already passed.')).apply(comms, caller)
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
    script.strike('worker', 'P1', f'P1 was struck for lateness against the wrong delivered commit in {repo}')
    script.unstrike('P1', 'P1 was delivered before its deadline; the commander recorded the later merge as the delivery')
    current = comms.goals.todos.get('P1')
    assert len(current.strikes) == 1 and script.struck(current, 'worker') == 0
    assert 'strike voided on P1' in (tmp_path / 'narrative.md').read_text()
    assert posted[-1][0] == VerdictAudience(('worker', 'lead')) and 'strike voided on P1' in posted[-1][1]
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
    assert script.held_orders() == () and not withdrawn.pending and script.struck(withdrawn, 'worker') == 0
    line = 'P1: order order-P1 withdrawn from worker, no strike: duplicate of P2, which owns the same fix (superseded by P2)'
    assert (tmp_path / 'board.md').read_text().count(line) == 1
    assert line in (tmp_path / 'narrative.md').read_text()
    assert [recipients for recipients, _ in posted] == [VerdictAudience(('worker', 'lead')), VerdictAudience(('worker',))]
    with pytest.raises(ValueError, match='Only an undelivered order'):
        script.withdraw('P1', 'withdrawing the same order twice records nothing new')


FIRED = f'The unit took two strikes on this problem and its second delivery of {__file__} was late again.'


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


@pytest.mark.parametrize('claim', [False, True])
def test_published_unit_jargon_is_corrected_on_its_open_order(commander, claim):
    """No watch runs: publishing the message is what records the correction on the order."""
    comms, _ = commander
    comms.messaging.initialize_private_initial_protocol()
    create(commander, 'P1')
    from agent_comms.envelope_claim_transitions import WritableFileClaim
    claims = (WritableFileClaim(Path('value.py')),) if claim else ()
    vague = comms.messaging.send('worker', 'lead', 'The canonical ownership changed', claims=claims).message.message_id
    comms.messaging.send('worker', 'lead', 'The tests pass now.').message.message_id
    comms.messaging.send('lead', 'worker', 'Your lifecycle report is late.').message.message_id
    corrected = comms.goals.todos.get('P1')
    assert [(c.message_id, c.words) for c in corrected.corrections] == [(vague, ('canonical', 'ownership'))]
    assert corrected.strike_count == 0


@pytest.mark.parametrize('claim', [False, True])
@pytest.mark.parametrize('body', ['The tests pass now.', 'The canonical ownership changed'])
def test_old_todo_schema_refuses_before_any_bus_publication(commander, claim, body):
    from agent_comms.envelope_claim_transitions import WritableFileClaim
    from agent_comms.errors import RelationViolationError

    comms, _ = commander
    comms.messaging.initialize_private_initial_protocol()
    create(commander, 'P1')
    with closing(sqlite3.connect(TodoStore.path_in(comms.root))) as db:
        for column in ('corrections', 'verdicts', 'released'):
            db.execute(f'ALTER TABLE todo DROP COLUMN {column}')
        db.commit()
    path = comms.root / 'bus.jsonl'
    before = path.read_bytes() if path.exists() else b''
    claims = (WritableFileClaim(Path('value.py')),) if claim else ()
    with pytest.raises(RelationViolationError, match='todos.sqlite3 differs from its declaration'):
        comms.messaging.send('worker', 'lead', body, claims=claims).message.message_id
    assert (path.read_bytes() if path.exists() else b'') == before


def test_committed_send_reports_correction_failure_to_tool_and_cli(commander, monkeypatch):
    import sqlite3
    from agent_comms.cli_commands import SendCliCommand
    from agent_comms.pi_payloads import PublishedMessageToolDetails
    from agent_comms.tools import invoke_tool

    comms, _ = commander
    comms.messaging.initialize_private_initial_protocol()
    create(commander, 'P1')

    def fail_correction(self, unit, message_id, text):
        assert message_id.encode() in (comms.root / 'bus.jsonl').read_bytes()
        raise sqlite3.OperationalError('P563 forced correction write failure')

    monkeypatch.setattr(TodoStore, 'correct', fail_correction)
    reply = invoke_tool(comms, 'comms_send', {
        'from': 'worker', 'to': 'lead', 'body': 'The canonical result is on the order'}, caller=commander[1])
    decoded = PublishedMessageToolDetails.from_wire(reply)
    assert decoded.id == decoded.message.source.message_id
    assert 'P563 forced correction write failure' in decoded.correction.error
    cli = SendCliCommand(sender='worker', target='lead', body='A separate fresh CLI message').apply(comms).encoded()
    assert 'was sent' in cli['message']
    assert 'P563 forced correction write failure' in cli['correction']['error']
    assert cli['id'] in cli['notices'][0]


@pytest.mark.parametrize('step, name, column', [
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
        todo.assignment, f'The unit delivered {__file__} after the recorded deadline had already passed.')).apply(comms, caller)
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
    standing = importlib.reload(importlib.import_module('agent_comms.commander.standing'))
    def stalled():
        return [rule.line() for rule in standing.RequestStalled.violations(comms, 'lead')]
    assert stalled() == [
        'REQUEST STALLED: request Owner request -> order its next step or mark it done']
    todo = create(commander, 'P1')
    lead = comms.registry.require('lead').incarnation
    assert todo.goal == GoalRef('lead', lead.created_at, 'request')
    assert stalled() == []
    with pytest.raises(ValueError, match='still has open orders'):
        request.RequestCompleteVerb(id='request', why='The owner request is finished').run()
    with pytest.raises(ValueError, match='still open'):
        request.RequestAddVerb(text='A second owner request while the first is open').run()
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert stalled() == [
        'REQUEST STALLED: request Owner request -> order its next step or mark it done']
    request.RequestCompleteVerb(id='request', why='The owner request is finished').run()
    assert stalled() == []


def test_a_claude_ai_directive_request_alarms_past_its_deadline_until_an_order_serves_it(
        commander, tmp_path, monkeypatch):
    """claude-ai's sends through the connector are directives: one to a thread holding owner requests
    is recorded on its open request with a deadline, or opens a new request once that one is done;
    a thread holding no request records nothing. Past its deadline it alarms until an order serves
    the request."""
    from agent_comms import standing_rules
    from agent_comms.connector import CALLER
    from agent_comms.goal_actions import CompletedGoalAction
    from agent_comms.goals import RequestDirective
    from agent_comms.rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry, RuleReviews
    from agent_comms.tools import CommsSendTool
    comms, caller = commander
    comms.registry.declare(comms.registry.new_thread('claude-ai', frozenset(), str(tmp_path), created_at=3.0))
    def reviewed():
        RuleReviews(comms.root).write('claude-ai', DrawnRuleReviewEntry(('Declare each fact once',), 1))
        RuleReviews(comms.root).write('claude-ai', AnsweredRuleReviewEntry(
            1, 'This tick moves rules.py:47 into one typed Core record that Core owns.'))
    def direct(target, body):
        CommsSendTool.invoke(comms, {'from': 'claude-ai', 'to': target, 'body': body}, caller=CALLER)
        return comms.bus.log.latest_sequence()
    def unacted(after):
        monkeypatch.setattr(standing_rules.time, 'time', lambda: after)
        return [rule.line() for rule in standing_rules.DirectiveUnacted.violations(comms, 'lead')]
    worker = comms.registry.require('worker').goal
    reviewed()
    seq = direct('lead', 'Make the slowdowns impossible by mechanism.')
    assert direct('worker', 'A note to a unit, not a request.') and comms.registry.require('worker').goal == worker
    goal = comms.registry.require('lead').goal
    (directive,) = goal.order.directives
    assert (goal.id, directive.seq, directive.served) == ('request', seq, 0)
    assert unacted(directive.deadline - 1) == []
    due = time.strftime('%H:%M', time.localtime(directive.deadline))
    assert unacted(directive.deadline + 1) == [
        f'DIRECTIVE UNACTED: claude-ai {seq} on request request, due {due}'
        ' -> order its next step or mark the request done']
    create(commander, 'P1')
    assert unacted(directive.deadline + 1) == []
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    comms.goals.update_goal('lead', CompletedGoalAction(progress='Served by P1.'))
    reviewed()  # the clock now stands past the first deadline
    seq = direct('lead', 'A second directive after the first request is done.')
    goal = comms.registry.require('lead').goal
    assert goal.id != 'request' and goal.state.active and goal.text == 'A second directive after the first request is done.'
    assert goal.order.directives == (RequestDirective(seq, goal.order.directives[0].deadline, 0),)
    assert unacted(goal.order.directives[0].deadline + 1) != []


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
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
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
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert comms.registry.require('worker').goal.state.active
    other = comms.goals.todos.get('P3')
    CommsOrderTool(owner='worker', order=OrderGoalAction(problem='P2', order=order(comms.root, 'revised'),
        request='request', deadline=RecordedEventTime(3000), commander='lead',
        expected_revision=comms.goals.todos.get('P2').revision)).apply(comms, caller)
    assert comms.goals.todos.get('P2').order.id == 'revised'
    assert comms.goals.todos.get('P3') == other
    CommsOrderDoneTool(problem='P2', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
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


def test_column_carries_run_before_order_validation(commander):
    """A retained store already has verdicts/releases; messages is added before orders read it."""
    comms, _ = commander
    completed_with_pending(commander)
    path = TodoStore.path_in(comms.root)
    with closing(sqlite3.connect(path)) as db:
        db.execute('ALTER TABLE todo DROP COLUMN messages')
        db.commit()
    with (closing(CoordinationStore.read_snapshot(path)) as db,
          pytest.raises(sqlite3.OperationalError, match='no such column: "messages"')):
        list(Todo.stream(db))
    repo = Path(__file__).parents[1]
    names = [step.name for step in CarryChain.at(repo, 'HEAD').steps]
    members = {'carry_todo_messages': ('TodoMessages', 3),
               'carry_todo_corrections': ('TodoCorrections', 0),
               'carry_order_run': ('OrderRuns', 0),
               'carry_todo_open_indexes': ('TodoOpenIndexes', 0)}
    assert [name for name in names if name in members] == list(members)
    for name, (member, changed) in members.items():
        carry = getattr(importlib.import_module('cutover.' + name), member)
        assert carry.carry(comms.root, apply=False).changed == changed
        assert carry.carry(comms.root, apply=True).changed == changed
        assert carry.carry(comms.root, apply=False).changed == 0
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
    comms.messaging.send('lead', 'parted', 'Are you there?').message.message_id
    rotate = importlib.reload(importlib.import_module('agent_comms.commander.rotate'))
    with comms.registry.external_process('watching', ProcessIdentity.capture(os.getpid())):
        standing = importlib.reload(importlib.import_module('agent_comms.commander.standing'))
        lines = [rule.line() for rule in standing.Detached.violations(comms, 'lead')]
    assert lines == ['DETACHED parted: 1 unread, orders P1 -> wake it or reassign']


def order_message(commander, problem, text):
    """The commander's bus message, recorded as the one the order was sent in."""
    comms, caller = commander
    message = comms.messaging.send('lead', 'worker', text).message.message_id
    CommsTodoTool(id=problem, operation=OrderMessage(
        comms.goals.todos.get(problem).revision, comms.registry.require('lead').incarnation, message)).apply(comms, caller)
    return message


def native_judge_edits(commander, judge, edits):
    """Use the actual native reader and original call IDs, never an edit journal."""
    comms, _ = commander
    path = comms.root / 'judge-session.jsonl'
    from datetime import datetime, timezone
    stamp = datetime.fromtimestamp(199., timezone.utc).isoformat()
    rows = [dict(type='session', id='judge-session', version=3, timestamp=stamp),
            dict(type='message', id='input', timestamp=stamp, message=dict(role='user', content='Repair the owner', timestamp=199000))]
    parent = 'input'
    for edit in edits:
        entry = 'entry-' + edit['id']
        rows.append(dict(type='message', id=entry, parentId=parent,
            timestamp=datetime.fromtimestamp(max(200., edit['at']), timezone.utc).isoformat(), message=dict(
            role='assistant', timestamp=max(200., edit['at']) * 1000, stopReason='toolUse', content=[dict(
                type='toolCall', id=edit['id'], name='edit', arguments=dict(path=edit['path'],
                edits=[dict(oldText=h['old'], newText=h['new']) for h in edit['hunks']]))])))
        parent = entry
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    path.chmod(0o600)
    comms.registry.register(replace(comms.registry.require('worker'), session_file=str(path)))
    return {edit.id: edit for edit in judge.Edit.unjudged(comms, 'lead')}


def test_a_verdict_lands_on_the_order_its_message_was_sent_in(commander, commander_script):
    """The cited message's recorded order receives the verdict, whatever P-number its text names."""
    import json
    from pathlib import Path

    comms, _ = commander
    create(commander, 'P1')
    create(commander, 'P2')
    judge = importlib.reload(importlib.import_module('agent_comms.commander.judge'))
    edit = dict(id='edit-1', at=1.0, thread='worker', path='value.py', hunks=[dict(old='value = 1', new='value = 2')])
    recorded = native_judge_edits(commander, judge, [edit])
    message = order_message(commander, 'P1', 'P2 was the last order; this one repairs the existing owner')
    judge.OkVerb(ids=(f'edit-1:{recorded[edit['id']].code()}',), msg=message,
                 why='The change repairs the existing canonical state owner',
                 serves='Repair the existing owner', lines=('value = 2',)).run()
    assert [v.tool_call_id for v in comms.goals.todos.get('P1').verdicts] == ['edit-1']
    assert comms.goals.todos.get('P2').verdicts == ()


def test_judge_ok_writes_the_todo_not_a_second_store(commander, commander_script):
    import json
    import time
    from pathlib import Path

    comms, _ = commander
    create(commander, 'P1')
    judge = importlib.reload(importlib.import_module('agent_comms.commander.judge'))
    edits = [dict(id=f'edit-{n}', at=time.time(), thread='worker', path='value.py',
                  hunks=[dict(old='value = 1', new='value = 2')]) for n in range(2)]
    recorded = native_judge_edits(commander, judge, edits)
    message = order_message(commander, 'P1', 'P1 Repair the existing owner')
    judge.OkVerb(ids=tuple(f'{e["id"]}:{recorded[e["id"]].code()}' for e in edits),
                 why='The changes repair the existing canonical state owner', msg=message,
                 serves='Repair the existing owner', lines=('value = 2',)).run()
    saved = comms.goals.todos.get('P1')
    assert [v.tool_call_id for v in saved.verdicts] == ['edit-0', 'edit-1']
    assert all(isinstance(v, AcceptedVerdict) for v in saved.verdicts)
    assert not saved.strikes
    assert importlib.reload(judge).Edit.unjudged(comms, 'lead') == []
    assert not (judge.CONFIG.state / 'verdicts.jsonl').exists()


@pytest.mark.parametrize('verb', ['ok', 'back'])
def test_judge_refuses_an_edit_without_an_open_order(commander, commander_script, verb):
    import json
    from pathlib import Path

    comms, _ = commander
    judge = importlib.reload(importlib.import_module('agent_comms.commander.judge'))
    edit = dict(id='orphan-edit', at=1.0, thread='worker', path='value.py', hunks=[dict(old='value = 1', new='value = 2')])
    recorded = native_judge_edits(commander, judge, [edit])
    fields = dict(ids=('orphan-edit:0000',),
                  why='The changes repair the existing canonical state owner', lines=('value = 2',),
                  msg=comms.messaging.send('lead', 'worker', 'P1 Repair the existing owner').message.message_id)
    command = judge.OkVerb(**fields, serves='Repair the existing owner') if verb == 'ok' else judge.BackVerb(
        **fields, roast='r', fix='f', slop_kind='state', fact='the one value being stored', at=f'{__file__}:1',
        derive='delete-copy', by=f'{__file__}:1')
    with pytest.raises(SystemExit, match=r'unknown edit ids.*orphan-edit'):
        command.run()
    assert not (judge.CONFIG.state / 'verdicts.jsonl').exists()


def test_judge_back_writes_one_strike_and_posts_the_roast(commander, commander_script, monkeypatch):
    import json
    from pathlib import Path

    comms, _ = commander
    create(commander, 'P1')
    judge = importlib.reload(importlib.import_module('agent_comms.commander.judge'))
    edit = dict(id='bad-edit', at=1.0, thread='worker', path='value.py', hunks=[dict(old='value = 1', new='value = 2')])
    recorded = native_judge_edits(commander, judge, [edit])
    message = order_message(commander, 'P1', 'P1 Repair the existing owner')
    posted = []
    monkeypatch.setattr(judge.order, 'post', lambda to, body: posted.append((to, body)) or 'sent')
    why = 'The unit copied the same state into a second writable store'
    judge.BackVerb(ids=(f'bad-edit:{recorded[edit['id']].code()}',), why=why, msg=message,
        lines=('value = 2',), roast='This is stupid because the unit copied the same state instead of using its existing owner',
        fix='Delete the copied state and read the value from its existing owner',
        slop_kind='state', fact='the one value being stored', at=f'{__file__}:1',
        derive='delete-copy', by=f'{__file__}:1').run()
    saved = comms.goals.todos.get('P1')
    assert len(saved.verdicts) == len(saved.strikes) == 1
    assert isinstance(saved.verdicts[0], SendBackVerdict)
    worker = comms.registry.require('worker')
    assert why in GoalSegment.capture(worker, worker.goal, comms.goals).progress
    assert judge.Edit.unjudged(comms, 'lead') == []
    audience = SendBackVerdict.audience.of('worker', saved.goal)
    assert [to for to, _ in posted] == [audience, audience]
    assert not (judge.CONFIG.state / 'verdicts.jsonl').exists()


@pytest.mark.parametrize('kind', [AcceptedVerdict, SendBackVerdict])
def test_each_verdict_kind_reaches_its_unit_and_the_threads_whose_goal_the_order_serves(commander, kind):
    comms, _ = commander
    todo = create(commander, 'P1')
    assert kind.audience.of('worker', todo.goal).threads == ('worker', 'lead')
    own = GoalRef('worker', comms.registry.require('worker').created_at, 'goal')
    assert kind.audience.of('worker', own).threads == ('worker',)


def test_each_send_target_takes_its_send_audience_and_a_channel_send_wakes_only_it(commander):
    comms, caller = commander
    with pytest.raises(ValueError, match='declare its audience'):
        CommsSendTool(sender='lead', target='#all', body='Read the order').apply(comms, caller)
    for target, audience in (('worker', ()), ('#all', ('worker',))):
        CommsSendTool(sender='lead', target=target, body='Read the order', audience=audience).apply(comms, caller)
        with comms.bus.log.opened_wire_snapshot() as source:
            message, _ = list(source.public_records())[-1]
        assert tuple(mention.thread for mention in message.mentions) == audience


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
    why = f'The unit wrapped the old store in {__file__} in a forwarding class instead of deleting it.'
    sent_back = SendBackVerdict(todo.revision, comms.registry.require('lead').incarnation, todo.assignment,
                                'toolu_2', reason=StrikeReason(why))
    CommsVerdictTool(id='P1', operation=sent_back).apply(comms, caller)
    judged = comms.goals.todos.get('P1')
    assert (judged.verdicts, judged.revision, judged.strike_count) == ((sent_back,), todo.revision + 1, 1)
    assert judged.strikes[0].reason == why
    worker = comms.registry.require('worker')
    revised = worker.goal
    assert (revised.id, revised.revision, revised.progress) == (goal.id, goal.revision + 1, '')
    assert f'Sent back: {why}' in GoalSegment.capture(worker, revised, comms.goals).progress



def test_a_problem_goal_shows_only_its_live_orders_through_convert_deliver_send_back_and_block(commander):
    """The converted goal keeps no prose: each turn reads its pending Todos, and a model block is refused."""
    comms, caller = commander
    create(commander, 'P1')
    todo = create(commander, 'P2')
    worker = comms.registry.require('worker')
    goal = worker.goal
    assert (goal.text, goal.progress) == (ProblemGoalOrder.text, '')
    assert [line.split(':')[0] for line in GoalSegment.capture(worker, goal, comms.goals).progress.splitlines()] == ['P1', 'P2']
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    why = f'The unit kept a second copy of the goal text in {__file__} beside the first.'
    CommsVerdictTool(id='P2', operation=SendBackVerdict(todo.revision, comms.registry.require('lead').incarnation,
        todo.assignment, 'toolu_3', reason=StrikeReason(why))).apply(comms, caller)
    worker = comms.registry.require('worker')
    progress = GoalSegment.capture(worker, worker.goal, comms.goals).progress
    assert progress.splitlines() == ['P1: Delivered, waiting on install', f'P2: Open · Sent back: {why}']
    assert (worker.goal.text, worker.goal.progress) == (ProblemGoalOrder.text, '')
    with pytest.raises(ValueError, match='block the problem.s Todo, or standby with wait_for'):
        comms.goals.update_goal('worker', BlockedGoalAction(block_reason='Waiting on lead.'), actor=ModelInvocable)
    assert comms.registry.require('worker').goal == worker.goal

VERDICT_LINES = {
    'ok': "e1:ab --msg m1 --line one --why why --serves 'Repair the existing owner'",
    'ok-unowned': "e1:ab --msg m1 --line one --why why --serves 'Repair the existing owner' --no-owner proof",
    'back': "e1:ab --msg m1 --line one --why 'The unit wrapped the old store' --roast r --fix f --slop state"
            f" --fact 'four words of fact' --at {__file__}:1 --derive delete-copy --by b.py:2",
}


def test_each_verdict_kind_requires_its_facts_and_records_its_verdict(commander, commander_script):
    """Every verdict member refuses a line missing any fact it declares, and its verdict reads back from the order."""
    comms, caller = commander
    judge = importlib.reload(importlib.import_module('agent_comms.commander.judge'))
    assert set(judge.VerdictVerb.names()) == set(VERDICT_LINES)
    create(commander, 'P1')
    for name, line in VERDICT_LINES.items():
        argv = [name, *shlex.split(line)]
        for at in [i for i, token in enumerate(argv) if token.startswith('--')]:
            with pytest.raises(SystemExit):
                judge.JudgeVerb.parse(argv[:at] + argv[at + 2:], '')
        operation = judge.JudgeVerb.parse(argv, '').operation(comms.goals.todos.get('P1'), f'toolu_{name}')
        CommsVerdictTool(id='P1', operation=operation).apply(comms, caller)
        assert comms.goals.todos.get('P1').verdicts[-1] == operation


def test_close_quotes_the_works_line(commander):
    """Closing evidence must quote four consecutive words of the order's Works when run."""
    comms, caller = commander
    run = ApplicationOrderRun('The board shows the problem closed after the commander reopens the session')
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem='P1', order=replace(order(comms.root, 'order-P1'), run=run), request='request',
        deadline=RecordedEventTime(1000), commander='lead', repo='OpenHCSDev/agent-comms', text='Fix P1',
    )).apply(comms, caller)
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
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
    with pytest.raises(ValueError, match='printed 1, not its expected count 0'):
        CommsOrderDoneTool(problem='P520', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert comms.goals.todos.get('P520') == pending
    (copied / 'flag.py').unlink()
    callers.write_text('def read(v):\n    return int(v is not None)\n')
    CommsOrderDoneTool(problem='P520', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert not comms.goals.todos.get('P520').pending


def test_send_guard_refuses_a_raw_send_only_to_the_commanders_units(commander, commander_script, monkeypatch, capsys):
    """worker holds one of lead's problems, so it is reached through orders; the owner, claude-ai
    and a peer commander hold none and are messaged directly."""
    import io
    from agent_comms.commander import send_guard
    create(commander, 'P1')

    def guarded(*targets):
        command = 'agent-comms send --from lead ' + ' '.join(f'--to {target}' for target in targets) + ' --body hi'
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'tool_input': {'command': command}})))
        send_guard.main([])
        return capsys.readouterr().out

    assert '"deny"' in guarded('worker')
    assert '"deny"' in guarded('user', 'worker')
    for target in ('user', 'claude-ai', 'desktop-claude-code'):
        assert guarded(target) == ''


@pytest.mark.parametrize('command, kind', [
    ('bash ~/code/toad/relink.sh', 'install'),
    ('uv tool install --force .', 'install'),
    ('PYTHONPATH=src python tools/upgrade_bus.py --root ~/.agent-comms', 'maintenance'),
    ('systemctl --user restart agent-comms-connector.service', 'connector restart'),
    ('cd repo && python tools/cutover/carry_todo_messages.py --root r', 'carry'),
])
def test_tactical_refused_from_the_commander_session_only(commander, commander_script, monkeypatch, capsys,
                                                          command, kind):
    """lead's registered process runs in Claude session s1. With no install planned, tactical work
    run in s1 is refused with the instruction to plan one; once the install is planned for worker,
    it is refused with the order to send worker. The same command from another session, and an
    order whose body names the command, pass. Once lead's process is gone no session can be
    classified, so every session is refused with the read error."""
    import io, os, subprocess
    from dataclasses import replace
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.commander import send_guard
    from agent_comms.recorded_time import RecordedEventTime
    from agent_comms.todos import PlannedInstallDeadline
    comms, _ = commander
    process = subprocess.Popen(['sleep', '60'], env={**os.environ, 'CLAUDE_CODE_SESSION_ID': 's1'})
    try:
        comms.registry.declare(replace(comms.registry.require('lead'),
                                       process_identity=ProcessIdentity.capture(process.pid)))

        def guarded(command, session):
            hook = {'session_id': session, 'tool_input': {'command': command}}
            monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(hook)))
            send_guard.main([])
            return capsys.readouterr().out

        unplanned = json.loads(guarded(command, 's1'))['hookSpecificOutput']['permissionDecisionReason']
        assert f"{kind.capitalize()} is the installer's work, and no install is planned" in unplanned
        assert 'agent-comms-commander order install HH:MM UNIT' in unplanned
        PlannedInstallDeadline(RecordedEventTime(2000.0), 'worker').plan(comms.root)
        refused = json.loads(guarded(command, 's1'))['hookSpecificOutput']
        assert refused['permissionDecision'] == 'deny'
        assert f"{kind.capitalize()} is worker's work, as the planned installer" in refused['permissionDecisionReason']
        assert 'order send --to worker' in refused['permissionDecisionReason']
        assert guarded(command, 's2') == ''
        assert guarded(f'agent-comms-commander order send --to worker --body "run {command}"', 's1') == ''
    finally:
        process.kill()
        process.wait()
    unclassified = json.loads(guarded(command, 's2'))['hookSpecificOutput']['permissionDecisionReason']
    assert f'{kind.capitalize()} refused: the guard cannot tell whether this session is lead' in unclassified
    assert f'/proc/{process.pid}/environ' in unclassified


def test_a_send_quoted_in_a_message_body_is_not_a_raw_send(commander, commander_script, monkeypatch, capsys):
    import io
    from agent_comms.commander import send_guard
    create(commander, 'P1')
    for command in ('agent-comms send --from user --to lead --body "agent-comms send --from lead --to worker"',
                    'agent-comms send --from user --to lead --body "x; agent-comms send --from lead --to worker"'):
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'tool_input': {'command': command}})))
        send_guard.main([])
        assert capsys.readouterr().out == ''


def checkout_with_origin(root):
    """A checkout at root holding one unpushed commit, with a bare origin beside it."""
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    subprocess.run(['git', 'init', '-q', '--bare', str(root / 'origin.git')], check=True)
    git('init', '-q')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'work')
    git('remote', 'add', 'origin', str(root / 'origin.git'))
    return git


def push_work(git):
    git('push', '-q', 'origin', 'HEAD:refs/heads/work')
    git('fetch', '-q', 'origin')


def test_each_claim_without_artifact_is_refused_and_accepted_with_it(commander, tmp_path):
    """A goal report claiming merged, installed or working is refused, naming the artifact, until
    the commit is on origin, P1's delivery recorded its check result and the screenshot exists;
    the order delivery tool refuses the same claims the same way."""
    from agent_comms.goal_actions import ActiveGoalAction
    from agent_comms.progress_claims import InstalledClaim, MergedClaim, WorkingClaim
    from agent_comms.tools import CommsGoalTool
    comms, caller = commander
    create(commander, 'P1')

    git = checkout_with_origin(tmp_path)
    screenshot = tmp_path / 'working.png'

    def push():
        push_work(git)

    def deliver():
        CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)

    claims = []
    for claim, missing, provide in (
        (MergedClaim(git('rev-parse', 'HEAD')), 'a commit sha that resolves on origin', push),
        (InstalledClaim('P1'), 'the recorded check result of P1', deliver),
        (WorkingClaim(str(screenshot)), 'a screenshot path that exists',
         lambda: screenshot.write_bytes(b'png')),
    ):
        with pytest.raises(ValueError, match=re.escape(missing)):
            CommsGoalTool(goal_id='request', status=ActiveGoalAction, progress='claimed',
                          claims=(claim,)).apply(comms, caller)
        with pytest.raises(ValueError, match=re.escape(missing)):
            CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=(claim,)).apply(comms, caller)
        provide()
        claims.append(claim)
    report = CommsGoalTool(goal_id='request', status=ActiveGoalAction, progress='claimed', claims=tuple(claims))
    assert report.apply(comms, caller).goal.progress == 'claimed'


def test_a_done_report_with_no_claim_is_refused_and_a_merged_claim_passes_claim_required(commander, tmp_path):
    """A delivered order and a completed goal each stand only on an attached claim, never on prose;
    a merged claim on a commit that resolves on origin carries both. The lead's worktree holds the
    order's checkout, so both resolve the one repository."""
    from agent_comms.goal_actions import CompletedGoalAction
    from agent_comms.progress_claims import MergedClaim
    from agent_comms.todos import DeliveredProblemOrder
    from agent_comms.tools import CommsGoalTool
    comms, caller = commander
    create(commander, 'P1')
    git = checkout_with_origin(tmp_path)
    push_work(git)
    merged = (MergedClaim(git('rev-parse', 'HEAD')),)
    for report, refused in (
        (CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=()), 'A delivered report needs a claim'),
        (CommsGoalTool(goal_id='request', status=CompletedGoalAction, progress='done'), 'A completed report needs a claim'),
    ):
        with pytest.raises(ValueError, match=re.escape(refused)):
            report.apply(comms, caller)
    CommsOrderDoneTool(problem='P1', delivery=OperationDelivery(), claims=merged).apply(comms, caller)
    assert isinstance(comms.goals.todos.get('P1').order, DeliveredProblemOrder)
    report = CommsGoalTool(goal_id='request', status=CompletedGoalAction, progress='done', claims=merged)
    assert isinstance(report.apply(comms, caller).goal.state, CompletedGoal)


def test_a_strike_or_send_back_citing_a_report_instead_of_state_is_refused(commander, tmp_path):
    """Each Evidence member checks its citation exists; a board row, goal text or a missing
    commit is a report of state, so the strike and the send-back that strikes are refused."""
    comms, caller = commander
    repo = tmp_path / 'repo'
    git = ('git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t')
    subprocess.run(('git', 'init', '-q', str(repo)), check=True)
    for text in ('0', '1'):
        (repo / 'count.txt').write_text(text)
        subprocess.run((*git, 'add', 'count.txt'), check=True)
        subprocess.run((*git, 'commit', '-qm', text), check=True)
    first, last = subprocess.run((*git, 'rev-list', 'HEAD'), capture_output=True, text=True,
                                 check=True).stdout.split()[::-1]
    record = tmp_path / 'session.jsonl'
    record.write_text('{}\n{}\n')
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem='P1', order=order(repo, 'order-P1'), request='request',
        deadline=RecordedEventTime(1000), commander='lead', repo='OpenHCSDev/agent-comms',
        text='Fix P1')).apply(comms, caller)
    lead = comms.registry.require('lead').incarnation
    did = 'The unit left count.txt at zero and reported the problem fixed'

    def strike(reason):
        todo = comms.goals.todos.get('P1')
        return CommsTodoTool(id='P1', operation=StrikeTodo(todo.revision, lead, todo.assignment,
                                                            reason)).apply(comms, caller)

    def send_back(reason):
        todo = comms.goals.todos.get('P1')
        return CommsVerdictTool(id='P1', operation=SendBackVerdict(
            todo.revision, lead, todo.assignment, 'call-1', reason=StrikeReason(reason))).apply(comms, caller)

    for refused in (f'{did}, says the board row for P1', f'{did}, says the Goal text Fix P1',
                    f'{did}, says another agent', f'{did} at commit {"0" * 12}',
                    f'{did} in {first[:9]}..{first[:9]}', f'{did} at {record}@99',
                    f'{did} from pid 999999999', f'{did}, see /nonexistent/state'):
        for write in (strike, send_back):
            with pytest.raises(ValueError, match='Cite the state the strike stands on'):
                write(refused)
    assert comms.goals.todos.get('P1').strikes == ()
    for cited in (f'{did} at commit {last[:12]}', f'{did} in {first[:9]}..{last[:9]}',
                  f'{did} at {record}@3', f'{did} from pid {os.getpid()}', f'{did}, see {record}'):
        strike(cited)
        assert comms.goals.todos.get('P1').strikes[-1].reason == cited
    send_back(f'{did} at commit {last}')
    assert comms.goals.todos.get('P1').strikes[-1].reason == f'{did} at commit {last}'
