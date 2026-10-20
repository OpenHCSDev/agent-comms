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
from agent_comms.commander import order as commander_order
from agent_comms.commander.config import Repo
from agent_comms.channel_targets import CommandTarget
from agent_comms.child_process import BoundedRun
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.goal_actions import BlockedGoalAction, CompletedGoalAction, CreateProblemGoalAction, ModelInvocable, OrderGoalAction, SetGoalAction
from agent_comms.goal_states import CompletedGoal
from agent_comms.goals import CountCheckResult, OrderCheck, ProblemGoalOrder
from agent_comms.progress_claims import WorkingClaim
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import (CommsCorrectWordingTool, CommsSendTool, CommsFireTool, CommsOrderCheckTool, CommsOrderDoneTool, CommsOrderTool,
    CommsOrderWorkTool, CommsProblemCloseTool, CommsTodoCreateTool, CommsTodoListTool, CommsChangeProblemTool, CommsVerdictTool)
from agent_comms.todos import (AwaitingFirstCall, VerdictAudience, OrderMessage, OrderDeletion, ModuleDeletion, NoOrderDeletion, GoalRef, CleanHunt, CommitDelivery, FirstCall, Hunt, OrderDelivery,
    ApplicationOrderRun, RecordedCommitDelivery, MechanicalOrderRun, TodoStore,
    LateObservedFirstCall, OperationDelivery, OrderedHunt,
    NotifiedFirstCallEvent, ObservedFirstCallEvent, PendingProblemOrder, RecordFirstCallTodo,
    AcceptedVerdict, Assignment, AssignTodo, DoneTodoState, ReleaseTodo, SendBackVerdict, StrikeReason,
    StrikeTodo, Todo, TodoConflict, TodoError, VoidStrikeTodo, Slop, DeclarationDerivation)
from agent_comms.turn_context import GoalSegment


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
        expected_revision=todo.revision, assigner=comms.registry.require('lead').incarnation)).apply(comms, caller)
    assert comms.registry.require('worker').goal.text == ProblemGoalOrder.text
    worker = comms.registry.require('worker').incarnation
    rows, rendered = comms.goals.todos.for_goal(worker, goal.id), '\n'.join(goal.instructions_for(comms.goals, worker))
    assert 'printf 7' in rendered and 'printf 1' not in rendered
    assert rows[0].deadline == RecordedEventTime(3000)


def test_strike_time_and_clean_ordered_hunts_keep_their_owners(commander):
    comms, caller = commander
    todo = create(commander, 'P1')
    actor = comms.registry.require('lead').incarnation
    CommsChangeProblemTool(problem='P1', operation=StrikeTodo(todo.revision, actor, todo.assignment,
        'The assigned change broke the existing resource release path during review.', RecordedEventTime(500))).apply(comms, caller)
    current = comms.goals.todos.get('P1')
    assert current.strikes[0].at == RecordedEventTime(500)
    assert current.strikes[0].actor == actor
    why = 'This source reads the same rows on every event without changing any owned fact.'
    for hunt in (CleanHunt(FileLine(__file__, 1), why, RecordedEventTime(600)),
                 OrderedHunt(FileLine(__file__, 1), why, RecordedEventTime(700), 'order-P1')):
        comms.goals.todos.record_hunt(actor, hunt)
        assert FieldCodec.decode(Hunt, FieldCodec.encode(hunt)) == hunt
    assert len(comms.goals.todos.hunts_for_commander(actor)) == 2


def test_delivery_checks_once_and_preserves_the_units_goal(commander):
    comms, caller = commander
    marker = comms.root / 'check-calls'
    create(commander, 'P1', f"printf x >> '{marker}'; printf 0; exit 1", 0)
    create(commander, 'P2')
    original_goal = comms.registry.require('worker').goal
    CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert marker.read_text() == 'x'
    assert comms.registry.require('worker').goal.state.active
    assert not comms.goals.todos.get('P1').pending
    assert comms.goals.todos.get('P1').order.delivery.at.at > 0
    goal = comms.registry.require('worker').goal
    rendered = '\n'.join(goal.instructions_for(comms.goals, comms.registry.require('worker').incarnation))
    assert 'Problem: P1' not in rendered and 'Problem: P2' in rendered
    CommsOrderDoneTool(problem='P2', order_id=comms.goals.todos.get('P2').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert comms.registry.require('worker').goal == original_goal


def test_failed_check_keeps_problem_pending(commander):
    comms, caller = commander
    original = create(commander, 'P1', 'printf 9', 1)
    with pytest.raises(ValueError, match=r'The order check is 8 away from CountCheckResult\(expected=1\)\.'):
        CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert comms.goals.todos.get('P1') == original
    assert comms.registry.require('worker').goal.state.active


def test_close_records_evidence_once_and_does_not_depend_on_artifact_lifetime(commander):
    comms, caller = commander
    create(commander, 'P1')
    CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
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
        CommsChangeProblemTool(problem='P1', operation=RecordFirstCallTodo(todo.revision,
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
    CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=CommitDelivery(commit=commit, delivered_commit=commit), claims=working(comms)).apply(comms, caller)
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
    git('update-ref', 'refs/remotes/origin/main', commit)
    marker = comms.root / 'check-calls'
    create(commander, 'P1', f"printf x >> '{marker}'; printf 1")
    create(commander, 'P2')
    CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=CommitDelivery(
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
        CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=CommitDelivery(commit=merge, delivered_commit=delivered), claims=working(comms)).apply(comms, caller)
    git('update-ref', 'refs/remotes/origin/work', delivered)
    git('update-ref', 'refs/remotes/origin/main', merge)
    script.done('P1', delivered, seen=working(comms)[0].screenshot)
    met = comms.goals.todos.get('P1')
    assert (met.order.delivery.commit, met.order.delivery.delivered, met.order.delivery.at) == (
        merge, delivered, RecordedEventTime(1000))
    assert commander_order.struck(met, 'worker') == 0

    git('checkout', '-q', '-b', 'side', delivered)
    git('commit', '-q', '--allow-empty', '-m', 'never merged', at=1500)
    side = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/side', side)
    git('checkout', '-q', 'main')
    git('commit', '-q', '--allow-empty', '-m', 'delivered after the deadline', at=3000)
    late = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', late)
    with pytest.raises(ValueError, match='not in the checked commit'):
        CommsOrderDoneTool(problem='P2', order_id=comms.goals.todos.get('P2').order.id, delivery=CommitDelivery(commit=late, delivered_commit=side), claims=working(comms)).apply(comms, caller)
    with pytest.raises(SystemExit, match='not in .* HEAD'):
        script.done('P2', side, seen=working(comms)[0].screenshot)
    script.done('P2', late, seen=working(comms)[0].screenshot)
    struck = comms.goals.todos.get('P2')
    assert commander_order.struck(struck, 'worker') == 0


def test_delivery_retains_commit_seconds_without_inventing_deadline_strikes(commander, commander_script):
    """Recording delivery retains its exact commit time; only progress can prove no work."""
    comms, _ = commander
    script, _ = commander_script

    def git(*args, at=None):
        env = {**os.environ, 'GIT_AUTHOR_DATE': f'@{at} +0000', 'GIT_COMMITTER_DATE': f'@{at} +0000'} if at else None
        return subprocess.check_output(('git', '-C', str(comms.root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                                        *args), env=env, stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q', '-b', 'main')
    git('commit', '-q', '--allow-empty', '-m', 'base', at=500)
    for pid, at in (('P1', 2000), ('P2', 2035)):
        create(commander, pid, deadline=2000)
        git('commit', '-q', '--allow-empty', '-m', f'{pid} delivered', at=at)
        delivered = git('rev-parse', 'HEAD')
        git('update-ref', 'refs/remotes/origin/main', delivered)
        script.done(pid, delivered, seen=working(comms)[0].screenshot)
    assert commander_order.struck(comms.goals.todos.get('P1'), 'worker') == 0
    delivered_order = comms.goals.todos.get('P2').order
    assert delivered_order.delivery.at == RecordedEventTime(2035)
    assert comms.goals.todos.get('P2').strikes == ()


def test_progress_owns_deadline_strikes_not_delivery_records(commander):
    from agent_comms.todos import MissedDeadlineStrike, PullRequests
    from agent_comms.thread_identity import StallWatcherIncarnation
    comms, _ = commander
    todo = create(commander, 'P1', deadline=2000)
    (late,) = MissedDeadlineStrike.observe(comms.goals, todo, 2035, PullRequests((todo,)))
    assert (late.previous, late.actor, late.expected_revision) == (
        todo.held(), StallWatcherIncarnation.watcher(), todo.revision)
    assert 'late by 35 s' in late.reason
    assert MissedDeadlineStrike.observe(comms.goals, todo, 2000, PullRequests((todo,))) == ()


@pytest.mark.parametrize('retained_problem', [None, 'P197', 'P1970'])
def test_done_distinguishes_non_git_home_from_exact_order_checkout(
        commander, commander_script, tmp_path, retained_problem):
    from agent_comms.storage_rules import StorageRule, UnitStorage

    comms, _ = commander
    script, _ = commander_script
    home = tmp_path / 'P197-application'
    home.mkdir()
    comms.registry.declare(comms.registry.new_thread('scratch', frozenset({'comms'}), str(home)))
    rule = StorageRule(checkout_base=tmp_path / 'wt', scratch_base=tmp_path / 'scratch', floor_gib=0)
    comms.registry.storage_rule = script.comms.registry.storage_rule = rule
    if retained_problem:
        repository = tmp_path / 'repository'
        subprocess.run(('git', 'init', '-q', str(repository)), check=True)
        subprocess.run(('git', '-C', str(repository), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-q', '--allow-empty', '-m', 'base'), check=True)
        subprocess.run(('git', '-C', str(repository), 'worktree', 'add', '-q', '-b', 'work',
                        str(rule.order_checkout('scratch', retained_problem))), check=True)
    todo = create(commander, 'P197', owner='scratch', deadline=time.time() + 3600)
    assert not (home / '.git').exists()
    assert UnitStorage.read(rule, comms.registry.require('scratch')).checkouts[0] == home
    if retained_problem == 'P197':
        with pytest.raises(SystemExit, match='still has the checkout'):
            script.done('P197', 'op', seen=working(comms)[0].screenshot)
        assert comms.goals.todos.get('P197').pending
    else:
        script.done('P197', 'op', seen=working(comms)[0].screenshot)
        assert not comms.goals.todos.get('P197').pending


def test_a_verified_commit_delivery_does_not_invent_a_deadline_strike(commander):
    """Delivered source is work; its recording path never creates an absence-of-work strike."""
    comms, caller = commander

    def git(*args, at=1000):
        return subprocess.check_output(
            ('git', '-C', str(comms.root), '-c', 'user.name=t', '-c', 'user.email=t@t', *args),
            env={**os.environ, 'GIT_COMMITTER_DATE': f'@{at} +0000'},
            stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q', '-b', 'main')
    git('commit', '-q', '--allow-empty', '-m', 'on time', at=1990)
    on_time = git('rev-parse', 'HEAD')
    git('commit', '-q', '--allow-empty', '-m', 'late', at=2035)
    late = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', late)

    def done(problem, commit):
        create(commander, problem, deadline=2000)
        CommsOrderDoneTool(problem=problem, order_id=comms.goals.todos.get(problem).order.id,
                           delivery=CommitDelivery(commit=commit, delivered_commit=commit),
                           claims=working(comms)).apply(comms, caller)
        return comms.goals.todos.get(problem)

    assert done('P1', on_time).strikes == ()
    delivered = done('P2', late)
    assert not delivered.pending and delivered.strikes == ()


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
        CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=CommitDelivery(commit=merge, delivered_commit=merge), claims=working(comms)).apply(comms, caller)
    answer = git('rev-parse', 'work')
    CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=CommitDelivery(commit=merge, delivered_commit=answer), claims=working(comms)).apply(comms, caller)
    assert not comms.goals.todos.get('P1').pending


def test_a_done_record_from_before_the_split_reads_its_one_commit_as_both():
    old = {'kind': 'commit', 'commit': 'd012d23b4', 'at': FieldCodec.encode(RecordedEventTime(1000))}
    recorded = FieldCodec.decode(OrderDelivery, old)
    assert type(recorded) is RecordedCommitDelivery and recorded.delivered == recorded.commit == 'd012d23b4'
    assert FieldCodec.encode(recorded) == old


def test_delivery_time_has_its_own_named_requirement():
    with pytest.raises(ValueError, match='delivery'):
        UnrecordedEventTime().require_delivery()


def test_slop_uses_every_pinned_catalog_entry_and_refuses_unknown_ids():
    location = FileLine(__file__, 1)
    for pattern in Slop.patterns():
        slop = Slop(pattern, 'the count of open orders', location, DeclarationDerivation, location)
        assert FieldCodec.decode(Slop, FieldCodec.encode(slop)) == slop
        assert pattern in slop.render()
    with pytest.raises(ValueError, match='MADEUP-999'):
        Slop('MADEUP-999', 'the count of open orders', location, DeclarationDerivation, location)


def test_order_send_refuses_unknown_pattern_before_recording(commander_script):
    script, repo = commander_script
    body = Path(order_body(repo))
    document = json.loads(body.read_text())
    document['slop'] = [dict(pattern='MADEUP-999', fact='the count of open orders',
                             at=f'{__file__}:1', derive='declaration', by=f'{__file__}:1')]
    body.write_text(json.dumps(document))
    with pytest.raises(SystemExit, match='MADEUP-999'):
        problem(script, str(body))
    assert script.held_orders() == ()
    judge = importlib.reload(importlib.import_module('agent_comms.commander.judge'))
    with pytest.raises(ValueError, match='MADEUP-999'):
        judge.BackVerb(ids=('edit:1234',), msg='m1', lines=('one',), why='wrong', roast='r', fix='f',
                       pattern='MADEUP-999', fact='the count of open orders', at=f'{__file__}:1',
                       derive='declaration', by=f'{__file__}:1').slop()


def test_commander_script_records_every_order_step_in_core(commander, commander_script):
    """problem, send, strike, hunt, done and close each change the Todo in Core; no order state is written to files."""
    comms, _ = commander
    script, repo = commander_script
    git = lambda *a: subprocess.run(['git', '-C', str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()
    (repo / 'count.txt').write_text('0\n')
    git('add', '.')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'base')
    body = Path(order_body(repo))
    due = '23:59'
    script.problem(str(body), 'worker', due, f'{__file__}:1')
    created = comms.goals.todos.get('P1')
    assert (created.assignment.owner, created.order.edits, created.order.banned) == (
        'worker', ('Set count.txt to 1.',), ('no second store', 'no cache'))

    document = json.loads(body.read_text())
    document['problems'] = [created.id]
    numbered = json.dumps(document)
    # The refusal itself supplies the exact replacement argument; no DM lookup.
    with pytest.raises(SystemExit) as refusal:
        script.send('worker', due, commander_order.OrderFile.read(numbered))
    expected = f"worker already holds P1; pass --replaces {created.messages[-1].message_id}"
    assert expected in str(refusal.value)
    replaces = str(refusal.value).split('--replaces ', 1)[1].splitlines()[0]
    print(f'Refused order: {refusal.value}')
    # Without --clean this replaces the prior order and counts a send-back.
    script.send('worker', due, commander_order.OrderFile.read(numbered), replaces=replaces)
    sent_back = comms.goals.todos.get('P1')
    assert sent_back.order.id != created.order.id and commander_order.struck(sent_back, 'worker') == 1
    print(f'Accepted replacement: {sent_back.order.id}')
    # A historical send cannot replace the latest order.
    with pytest.raises(SystemExit, match=sent_back.messages[-1].message_id):
        script.send('worker', due, commander_order.OrderFile.read(numbered), replaces=replaces)
    with pytest.raises(ValueError, match='no order message was recorded'):
        replace(sent_back, messages=()).require_replacement(replaces)
    (link,) = sent_back.messages[-1:]
    sent = next(m.body for m in comms.views.history_page(CommandTarget.named('worker'), viewer='lead').messages if m.message_id == link.message_id)
    assert 'Set count.txt to 1.' in sent and 'Start now and push as you go.' in sent
    assert 'Due' not in sent and due not in sent  # the deadline stays the commander's fire line

    script.hunt(f'{__file__}:1', 'the counter is reread per event and the order replaces it with one read', sent_back.order.id)
    assert [h.sent for h in comms.goals.todos.hunts_for_commander(comms.registry.require('lead').incarnation)] == [sent_back.order.id]

    (repo / 'count.txt').write_text('1\n')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'fix')
    git('update-ref', 'refs/remotes/origin/main', 'HEAD')
    script.done('P1', git('rev-parse', 'HEAD'), seen=working(comms)[0].screenshot)
    assert not comms.goals.todos.get('P1').pending

    artifact = repo.parent / 'seen.png'
    artifact.write_text('x')
    script.close('P1', 'the installed app shows the counter at one after restart', str(artifact))
    assert not comms.goals.todos.get('P1').state.open and script.held_orders() == ()
    assert not {'problems.json', 'deadlines.json', 'closed.jsonl', 'strikes.jsonl', 'done.jsonl',
                'sent.jsonl', 'hunts.jsonl'} & {
        path.name for path in (repo.parent / 'state').iterdir()}


def order_body(repo, check=None, edit='Set count.txt to 1.', extra='', deletes=NoOrderDeletion()):
    """Raw JSON input; the application, not the test, constructs the order."""
    body = repo.parent / 'order.json'
    checked = OrderCheck.parse(f'{repo} :: {check or "cat count.txt -> 1"}')
    body.write_text(json.dumps({
        'defect': 'The counter file holds zero where the fleet needs one',
        'why_stupid': 'The counter is wrong and that is stupid.',
        'order': {'kind': 'pending', 'owner': f'{__file__}:1',
                  'check': FieldCodec.encode(checked), 'edits': [edit],
                  'deletes': FieldCodec.encode(deletes),
                  'run': {'kind': 'application', 'text': 'the installed app shows the counter at one after restart'},
                  'banned': ['no second store', 'no cache'],
                  'report': 'the commit and the count before -> after ' + extra}}))
    return str(body)


def problem(script, body):
    script.problem(body, 'worker', '23:59', f'{__file__}:1')


def test_installed_order_entrypoint_refuses_then_accepts_printed_replacement(
        commander, commander_script):
    from agent_comms.rule_reviews import (
        AnsweredRuleReviewEntry, DrawnRuleReviewEntry, RuleReviews,
    )

    comms, _ = commander
    script, repo = commander_script
    (repo / 'count.txt').write_text('0\n')
    body = Path(order_body(repo))
    problem(script, str(body))
    document = json.loads(body.read_text())
    document['problems'] = ['P1']
    body.write_text(json.dumps(document))
    reviews = RuleReviews(comms.root)
    reviews.write('lead', DrawnRuleReviewEntry(('Keep the current order message with its Todo.',)))
    reviews.write('lead', AnsweredRuleReviewEntry(
        1, 'Keep the current order message with its Todo. P1'))
    command = [str(Path(sys.executable).with_name('agent-comms-commander')),
               'order', 'send', 'worker', '23:59', str(body)]
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src'),
           'AGENT_COMMS_ROOT': str(comms.root)}
    refused = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0
    current = comms.goals.todos.get('P1')
    expected = f'worker already holds P1; pass --replaces {current.messages[-1].message_id}'
    assert expected in refused.stderr
    replacement = re.search(r'pass --replaces (\w+)', refused.stderr).group(1)
    print(expected)
    accepted = subprocess.run([*command, '--replaces', replacement], env=env,
                              capture_output=True, text=True, timeout=30)
    assert accepted.returncode == 0, accepted.stderr
    assert comms.goals.todos.get('P1').order.id != current.order.id
    print(f'Accepted --replaces {replacement}: {accepted.stdout.strip()}')


@pytest.mark.parametrize('spelling', ['agent-comms', '.', 'absolute'])
def test_check_repo_resolves_once_for_send_and_correction(
        commander, commander_script, monkeypatch, tmp_path, spelling):
    comms, _ = commander
    script, repo = commander_script
    script = script.model_copy(update={'repos': (Repo.of(repo),)})
    monkeypatch.chdir(repo)
    # A same-named directory must not override the configured repository.
    (repo / 'agent-comms').mkdir()
    (repo / 'count.txt').write_text('0\n')
    marker = tmp_path / 'check-cwd'
    command = f'pwd > {shlex.quote(str(marker))}; cat count.txt'
    git = lambda *args: subprocess.run(
        ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
        check=True, capture_output=True, text=True).stdout.strip()
    git('add', 'count.txt')
    git('commit', '-qm', 'base')
    git('update-ref', 'refs/remotes/origin/main', 'HEAD')
    directory = str(repo) if spelling == 'absolute' else spelling
    body = order_body(repo, f'{command} -> 1')
    Path(body).write_text(Path(body).read_text().replace(f'"directory": "{repo}"', f'"directory": "{directory}"'))
    problem(script, body)
    sent = comms.goals.todos.get('P1')
    assert sent.order.check.directory == str(repo.resolve())
    assert marker.read_text().strip() == str(repo.resolve())
    script.correct_check('P1', f'{directory} :: {command} -> 0',
                         'The recorded expected count was wrong; the unchanged file contains zero.')
    corrected = comms.goals.todos.get('P1')
    assert corrected.order.id == sent.order.id
    assert corrected.order.check.directory == str(repo.resolve())
    assert marker.read_text().strip() == str(repo.resolve())


@pytest.mark.parametrize('entrypoint', ['send', 'correct'])
def test_missing_check_repo_refuses_before_recording(
        commander, commander_script, tmp_path, entrypoint):
    comms, _ = commander
    script, repo = commander_script
    (repo / 'count.txt').write_text('0\n')
    if entrypoint == 'correct':
        problem(script, order_body(repo))
    before = comms.goals.todos.for_commander(comms.registry.require('lead').incarnation)
    missing = tmp_path / 'missing-repo'
    with pytest.raises(SystemExit, match='is not a directory|directory does not exist'):
        if entrypoint == 'send':
            body = order_body(repo)
            Path(body).write_text(Path(body).read_text().replace(f'"directory": "{repo}"', f'"directory": "{missing}"'))
            problem(script, body)
        else:
            script.correct_check('P1', f'{missing} :: printf 0 -> 0',
                                 'The recorded check must run in the correct configured source checkout.')
    assert comms.goals.todos.for_commander(comms.registry.require('lead').incarnation) == before


def test_resend_after_strike_counts_one_miss(commander, commander_script, monkeypatch):
    """A miss struck with `order strike` and then sent back is one strike; the next send-back strikes again."""
    comms, _ = commander
    script, repo = commander_script
    monkeypatch.setattr(type(script), 'post', lambda self, recipients, body, answers=(): 'sent')
    (repo / 'count.txt').write_text('0\n')
    body = order_body(repo)
    problem(script, body)
    script.strike('worker', 'P1', 'P1 left count.txt at zero and reported it fixed')
    document = json.loads(Path(body).read_text())
    document['problems'] = ['P1']
    numbered = json.dumps(document)
    script.send('worker', '23:59', commander_order.OrderFile.read(numbered),
                replaces=comms.goals.todos.get('P1').messages[-1].message_id)
    assert commander_order.struck(comms.goals.todos.get('P1'), 'worker') == 1
    script.send('worker', '23:59', commander_order.OrderFile.read(numbered),
                replaces=comms.goals.todos.get('P1').messages[-1].message_id)
    assert commander_order.struck(comms.goals.todos.get('P1'), 'worker') == 2


def test_a_strike_and_a_clean_hunt_land_on_their_problem_argument_not_one_the_why_cites(
        commander, commander_script, monkeypatch):
    comms, _ = commander
    script, repo = commander_script
    monkeypatch.setattr(type(script), 'post', lambda self, recipients, body, answers=(): 'sent')
    (repo / 'count.txt').write_text('0\n')
    problem(script, order_body(repo))
    create(commander, 'P2', owner='worker', deadline=time.time() + 3600)
    script.strike('worker', 'P2', 'P1 was compared with this miss: P2 left its count at zero and reported it fixed')
    assert [commander_order.struck(comms.goals.todos.get(pid), 'worker') for pid in ('P1', 'P2')] == [0, 1]
    why = 'P1 rereads the counter per event; this read of the P2 path found it clean'
    script.hunt(f'{__file__}:1', why, '')
    assert comms.goals.todos.hunts_for_commander(comms.registry.require('lead').incarnation)[0].why == why
    done = replace(comms.goals.todos.get('P2'), state=DoneTodoState)
    with pytest.raises(TodoError, match='P2 is done'):
        done.state.require_open(done)


def test_a_deletion_order_must_check_the_imports_of_every_repo_that_imports_the_module(
        commander, commander_script, monkeypatch, tmp_path):
    """P12 deleted acp_ingress.py while Toad imported it; the check must read Toad's imports too."""
    comms, _ = commander
    script, repo = commander_script
    monkeypatch.setattr(type(script), 'post', lambda self, recipients, body, answers=(): 'sent')
    toad = tmp_path / 'toad'
    (toad / 'src').mkdir(parents=True)
    (toad / 'src' / 'app.py').write_text('from agent_comms.acp_ingress import Ingress\n')
    script = script.model_copy(update={'repos': (
        Repo(checkout=toad, github='OpenHCSDev/toad'),)})
    (repo / 'acp_ingress.py').write_text('class Ingress: ...\n')
    edit = 'Remove the obsolete ingress implementation.'
    deletes = ModuleDeletion(('src/agent_comms/acp_ingress.py',))
    with pytest.raises(SystemExit, match=rf"\['{toad}'\] import"):
        problem(script, order_body(repo, 'grep -rl Ingress . | wc -l -> 0', edit, deletes=deletes))
    problem(script, order_body(repo, f'grep -rl Ingress . {toad} | wc -l -> 0', edit, deletes=deletes))
    assert comms.goals.todos.get('P1').order.check.command == f'grep -rl Ingress . {toad} | wc -l'
    assert comms.goals.todos.get('P1').order.deletes == deletes
    assert deletes.instructions() == ('Deletes: src/agent_comms/acp_ingress.py',)


def test_an_order_changing_tools_must_ratchet_both_roots(commander, commander_script, monkeypatch):
    comms, _ = commander
    script, repo = commander_script
    monkeypatch.setattr(type(script), 'post', lambda self, recipients, body, answers=(): 'sent')
    (repo / 'count.txt').write_text('0\n')
    tools = repo / 'tools'
    tools.mkdir()
    measure = tools / 'debt_ratchet.py'
    measure.write_text('# Existing measure owner\n')
    edit = f'Count the measure at {measure}:1.'
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
    body = order_body(repo, 'grep -c two count.txt -> 1')
    problem(script, body)
    sent = comms.goals.todos.get('P1')
    corrected = f'{repo} :: cat count.txt -> 1'
    why = 'the recorded check greps a word the ordered edit never writes'
    with pytest.raises(ValueError, match='The order check is 1 away'):
        script.correct_check('P1', corrected, why)  # main still holds 0
    with pytest.raises(TodoConflict):
        CommsOrderCheckTool(problem='P1', check=OrderCheck(str(repo), 'cat count.txt', CountCheckResult(1)),
                            why=why).apply(comms, ThreadCaller('worker'))
    assert comms.goals.todos.get('P1') == sent

    (repo / 'count.txt').write_text('1\n')
    git('commit', '-qam', 'fix')
    git('update-ref', 'refs/remotes/origin/main', 'HEAD')
    fixed = git('rev-parse', 'HEAD')
    with pytest.raises(ValueError, match=r'The order check is 1 away from CountCheckResult\(expected=1\)\.'):
        script.done('P1', fixed, seen=working(comms)[0].screenshot)
    failed = comms.goals.todos.get('P1')
    from agent_comms.todos import FailedCheckStrike
    assert len(failed.strikes) == 1 and isinstance(failed.strikes[0], FailedCheckStrike)
    assert (failed.order.id, failed.assignment, failed.deadline) == (
        sent.order.id, sent.assignment, sent.deadline)
    script.correct_check('P1', corrected, why)
    current = comms.goals.todos.get('P1')
    assert (current.order.id, current.assignment, current.deadline, current.strikes) == (
        failed.order.id, failed.assignment, failed.deadline, failed.strikes)
    [record] = current.order.check_corrections
    assert (record.previous, record.check, record.reason) == (
        sent.order.check, OrderCheck(str(repo), 'cat count.txt', CountCheckResult(1)), why)
    script.done('P1', fixed, seen=working(comms)[0].screenshot)
    assert not comms.goals.todos.get('P1').pending
    assert comms.goals.todos.get('P1').order.check_corrections == (record,)


def test_commander_refuses_soft_prose_without_filtering_code(commander_script):
    script, repo = commander_script
    (repo / 'count.txt').write_text('0\n')
    body = Path(order_body(repo))
    document = json.loads(body.read_text())
    document['why_stupid'] = 'Maybe consider repairing the counter because the value is wrong.'
    document['order']['edits'] = ['Keep identifiers maybe_consider and optional_action unchanged.']
    body.write_text(json.dumps(document))
    original = body.read_bytes()
    with pytest.raises(SystemExit, match='why_stupid: Soft wording'):
        problem(script, str(body))
    assert script.held_orders() == () and body.read_bytes() == original


def test_commander_refuses_a_check_that_prints_no_count(commander_script):
    """done could never pass a check that prints no integer, so send refuses it before Core records anything."""
    script, repo = commander_script
    body = order_body(repo, 'git grep -c no-such-text -> 0')
    with pytest.raises(SystemExit, match='the check printed no integer'):
        problem(script, body)
    assert script.held_orders() == ()


@pytest.mark.parametrize('printed, refused', [
    ("printf 'no count here'", 'The order check cannot prove CountCheckResult(expected=1): it printed no integer.'),
    ('echo 2', 'The order check is 1 away from CountCheckResult(expected=1).'),
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
    CommsChangeProblemTool(problem='P1', operation=StrikeTodo(todo.revision, lead, todo.assignment,
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
    CommsChangeProblemTool(problem='P1', operation=VoidStrikeTodo(struck.revision, lead, strike, why)).apply(comms, caller)
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
    monkeypatch.setattr(type(script), 'post', lambda self, recipients, body, answers=(): posted.append((recipients, body)))
    script.strike('worker', 'P1', 'P1 was struck for lateness against the wrong delivered commit')
    script.unstrike('P1', 'P1 was delivered before its deadline; the commander recorded the later merge as the delivery')
    current = comms.goals.todos.get('P1')
    assert len(current.strikes) == 1 and commander_order.struck(current, 'worker') == 0
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
    monkeypatch.setattr(type(script), 'post', lambda self, recipients, body, answers=(): posted.append((recipients, body)))
    script.withdraw('P1', 'duplicate of P2, which owns the same fix', 'P2')
    withdrawn = comms.goals.todos.get('P1')
    assert script.held_orders() == () and not withdrawn.pending and commander_order.struck(withdrawn, 'worker') == 0
    line = 'P1: order order-P1 withdrawn from worker, no strike: duplicate of P2, which owns the same fix (superseded by P2)'
    assert (tmp_path / 'board.md').read_text().count(line) == 1
    assert line in (tmp_path / 'narrative.md').read_text()
    assert [recipients for recipients, _ in posted] == [VerdictAudience(('worker', 'lead')), VerdictAudience(('worker',))]
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
    CommsChangeProblemTool(problem='P1', operation=StrikeTodo(todo.revision, comms.registry.require('lead').incarnation,
        todo.assignment, FIRED)).apply(comms, commander[1])
    struck = comms.goals.todos.get('P1')
    def release():
        CommsChangeProblemTool(problem='P1', operation=ReleaseTodo(struck.revision, comms.registry.require(caller).incarnation,
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
    commander_order.FireVerb(unit='worker', why=FIRED).apply(script)
    assert [(t.assignment, t.released[0].reason.text) for t in map(comms.goals.todos.get, ('P1', 'P2'))] == [(None, FIRED)] * 2
    assert capsys.readouterr().out.splitlines() == [f'{pid}: released from worker: {FIRED}' for pid in ('P1', 'P2')]
    script.write_board()
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

    def fail_writer(self, *, check_same_thread=True):
        assert b'The canonical result is on the order' in (comms.root / 'bus.jsonl').read_bytes()
        raise sqlite3.OperationalError('P563 forced correction write failure')

    monkeypatch.setattr(TodoStore, '_open_writer', fail_writer)
    reply = invoke_tool(comms, 'comms_send', {
        'from': 'worker', 'to': 'lead', 'body': 'The canonical result is on the order'}, caller=commander[1])
    decoded = PublishedMessageToolDetails.from_wire(reply)
    assert decoded.id == decoded.message.source.message_id
    assert decoded.correction.error.startswith('TodoStoreUnwritable:')
    assert 'P563 forced correction write failure' in decoded.correction.error
    cli = SendCliCommand(sender='worker', target='lead',
                         body='A separate fresh CLI message uses canonical wording').apply(comms).to_wire()
    assert 'was sent' in cli['message']
    assert cli['correction']['error'].startswith('TodoStoreUnwritable:')
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
    CommsChangeProblemTool(problem='P1', operation=StrikeTodo(todo.revision, comms.registry.require('lead').incarnation,
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
    script, _ = commander_script
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
        request.RequestCompleteVerb(id='request', why='The owner request is finished').apply(script)
    with pytest.raises(ValueError, match='still open'):
        request.RequestAddVerb(text='A second owner request while the first is open').apply(script)
    CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert stalled() == [
        'REQUEST STALLED: request Owner request -> order its next step or mark it done']
    request.RequestCompleteVerb(id='request', why='The owner request is finished').apply(script)
    assert stalled() == []


def test_each_directive_requires_its_own_linked_answer(commander, tmp_path):
    """An unrelated order answers neither ask; an order linked to one answers only that ask."""
    from agent_comms.connector import CALLER
    from agent_comms.goals import RequestDirective

    comms, caller = commander
    comms.registry.declare(comms.registry.new_thread(
        'claude-ai', frozenset(), str(tmp_path), created_at=3.0))

    def direct(body):
        CommsSendTool.invoke(comms, {'from': 'claude-ai', 'to': 'lead', 'body': body}, caller=CALLER)
        return comms.bus.log.latest_sequence()

    first = direct('Make the slowdowns impossible by mechanism.')
    second = direct('Clear the board without losing the unfinished requests.')
    owner = comms.registry.require('lead').incarnation
    goal = comms.registry.require('lead').goal
    now = max(item.deadline for item in goal.order.directives) + 1

    def overdue():
        current = comms.registry.require('lead').goal
        return tuple(item.seq for item in current.order.overdue_directives(
            current, comms.goals, owner, now))

    assert overdue() == (first, second)
    create(commander, 'P1')
    assert overdue() == (first, second)
    original = goal.order.directives[0].original(comms.goals)
    reply = comms.messaging.send('lead', 'worker', 'P1 answers the slowdown directive.',
                                answers=(original.reference,)).message
    assert reply.answers == (original.reference,)
    assert overdue() == (second,)
    original = goal.order.directives[1].original(comms.goals)
    comms.messaging.send('lead', 'claude-ai', 'The board directive is answered separately.',
                         answers=(original.reference,))
    assert overdue() == ()
    CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id,
                      delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    comms.goals.update_goal('lead', CompletedGoalAction(progress='Served by P1.'))
    third = direct('A new directive after the original request is done.')
    goal = comms.registry.require('lead').goal
    assert goal.id != 'request' and goal.state.active
    assert goal.order.directives == (RequestDirective(third, goal.order.directives[0].deadline),)
    assert goal.order.directives[0].overdue(comms.goals, owner,
                                          goal.order.directives[0].deadline + 1)


def stray(commander, problem, generation):
    """A pending problem stored the way pre-P48 joins and refused sends left them: under a Goal id that is
    not the unit's open Goal."""
    comms, _ = commander
    lead = comms.registry.require('lead')
    return comms.goals.todos.create(problem, 'OpenHCSDev/agent-comms', 'Fix ' + problem, creator=lead,
        owner=comms.registry.require('worker'), generation=generation, deadline=RecordedEventTime(1000),
        order=order(comms.root, 'order-' + problem), goal=GoalRef('lead', lead.created_at, 'request'))


def test_imported_order_completion_uses_order_identity_and_keeps_goal(commander):
    comms, caller = commander
    todo = stray(commander, 'P603', 'rts-order:source')
    original_goal = comms.registry.require('worker').goal
    request = CommsOrderDoneTool(problem=todo.id, order_id=todo.order.id,
        delivery=OperationDelivery(), claims=working(comms))
    with pytest.raises(ValueError, match='order changed'):
        replace(request, order_id='replaced-order').apply(comms, caller)
    assert comms.goals.todos.get(todo.id) == todo
    request.apply(comms, caller)
    assert not comms.goals.todos.get(todo.id).pending
    assert comms.registry.require('worker').goal == original_goal
    with pytest.raises(ValueError, match='Only an open order'):
        request.apply(comms, caller)


def completed_with_pending(commander):
    """ci-1370's shape: the Goal completed on its only problem, then later problems were stored under it
    and under a Goal that was never published."""
    comms, caller = commander
    create(commander, 'P1')
    CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    # This fixture models a retained pre-fix registry, not a new completion.
    with comms.registry.store.editing() as edit:
        worker = edit.document.threads['worker']
        edit.document.threads['worker'] = replace(worker, goal=replace(worker.goal, state=CompletedGoal()))
        edit.commit()
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
    CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert comms.registry.require('worker').goal.state.active
    other = comms.goals.todos.get('P3')
    CommsOrderTool(owner='worker', order=OrderGoalAction(problem='P2', order=order(comms.root, 'revised'),
        request='request', deadline=RecordedEventTime(3000), commander='lead',
        expected_revision=comms.goals.todos.get('P2').revision,
        assigner=comms.registry.require('lead').incarnation)).apply(comms, caller)
    assert comms.goals.todos.get('P2').order.id == 'revised'
    assert comms.goals.todos.get('P3') == other
    CommsOrderDoneTool(problem='P2', order_id=comms.goals.todos.get('P2').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert comms.registry.require('worker').goal.state.active


def test_a_refused_send_leaves_the_todo_row_byte_identical(commander):
    comms, caller = commander
    goal = completed_with_pending(commander)
    before = todo_row(comms, 'P3')
    with pytest.raises(ValueError, match='Finish or release the assigned problems'):
        CommsOrderTool(owner='worker', order=OrderGoalAction(problem='P3', order=order(comms.root, 'resent'),
            request='request', deadline=RecordedEventTime(3000), commander='lead',
            expected_revision=comms.goals.todos.get('P3').revision,
            assigner=comms.registry.require('lead').incarnation)).apply(comms, caller)
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
    members = {'carry_todo_corrections': ('TodoCorrections', 0),
               'carry_todo_messages': ('TodoMessages', 3),
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


def test_dropped_problem_links_through_the_unordered_lifecycle(commander):
    from agent_comms.todos import LocatedProblemFinding, UnlocatedProblemFinding
    comms, caller = commander
    original_finding = LocatedProblemFinding(FileLine(__file__, 1))
    for pid, at, finding in (('P1', 100, original_finding), ('P2', 200, UnlocatedProblemFinding())):
        CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
            problem=pid, order=replace(order(comms.root, 'order-' + pid),
                at=RecordedEventTime(at), first_call_deadline=RecordedEventTime(at + 120), finding=finding),
            request='request', deadline=RecordedEventTime(1000), commander='lead',
            repo='OpenHCSDev/agent-comms', text='Fix ' + pid)).apply(comms, caller)
    old_message = order_message(commander, 'P1', 'The earlier counter order')
    new_message = order_message(commander, 'P2', 'The newer counter order')
    lead = comms.registry.require('lead').incarnation
    comms.goals.todos.drop(lead, 'worker', old_message, new_message)
    dropped = comms.goals.todos.get('P1')
    assert dropped.holder is None and not dropped.pending
    comms.goals.todos.link(lead, 'P1', 'worker', new_message)
    linked = comms.goals.todos.get('P1')
    assert linked.holder == 'worker' and linked.pending
    assert linked.order.id == comms.goals.todos.get('P2').order.id
    assert linked.order.finding == original_finding
    assert comms.goals.todos.get('P2').order.finding == UnlocatedProblemFinding()


def order_message(commander, problem, text):
    """The commander's bus message, recorded as the one the order was sent in."""
    comms, caller = commander
    message = comms.messaging.send('lead', 'worker', text).message.message_id
    CommsChangeProblemTool(problem=problem, operation=OrderMessage(
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
    script, _ = commander_script
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
                 serves='Repair the existing owner', lines=('value = 2',)).apply(script)
    assert [v.tool_call_id for v in comms.goals.todos.get('P1').verdicts] == ['edit-1']
    assert comms.goals.todos.get('P2').verdicts == ()


def test_judge_ok_writes_the_todo_not_a_second_store(commander, commander_script):
    script, _ = commander_script
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
                 serves='Repair the existing owner', lines=('value = 2',)).apply(script)
    saved = comms.goals.todos.get('P1')
    assert [v.tool_call_id for v in saved.verdicts] == ['edit-0', 'edit-1']
    assert all(isinstance(v, AcceptedVerdict) for v in saved.verdicts)
    assert not saved.strikes
    assert importlib.reload(judge).Edit.unjudged(comms, 'lead') == []
    assert not (script.state / 'verdicts.jsonl').exists()


@pytest.mark.parametrize('verb', ['ok', 'back'])
def test_judge_refuses_an_edit_without_an_open_order(commander, commander_script, verb):
    script, _ = commander_script
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
        **fields, roast='r', fix='f', pattern='IDEN-5', fact='the one value being stored', at=f'{__file__}:1',
        derive='delete-copy', by=f'{__file__}:1')
    with pytest.raises(SystemExit, match=r'unknown edit ids.*orphan-edit'):
        command.apply(script)
    assert not (script.state / 'verdicts.jsonl').exists()


def test_judge_back_writes_one_strike_and_posts_the_roast(commander, commander_script, monkeypatch):
    script, _ = commander_script
    import json
    from pathlib import Path

    comms, _ = commander
    create(commander, 'P1')
    judge = importlib.reload(importlib.import_module('agent_comms.commander.judge'))
    edit = dict(id='bad-edit', at=1.0, thread='worker', path='value.py', hunks=[dict(old='value = 1', new='value = 2')])
    recorded = native_judge_edits(commander, judge, [edit])
    message = order_message(commander, 'P1', 'P1 Repair the existing owner')
    posted = []
    monkeypatch.setattr(type(script), 'post', lambda self, to, body, answers=(): posted.append((to, body)) or 'sent')
    why = 'The unit copied the same state into a second writable store'
    judge.BackVerb(ids=(f'bad-edit:{recorded[edit['id']].code()}',), why=why, msg=message,
        lines=('value = 2',), roast='This is stupid because the unit copied the same state instead of using its existing owner',
        fix='Delete the copied state and read the value from its existing owner',
        pattern='IDEN-5', fact='the one value being stored', at=f'{__file__}:1',
        derive='delete-copy', by=f'{__file__}:1').apply(script)
    saved = comms.goals.todos.get('P1')
    assert len(saved.verdicts) == len(saved.strikes) == 1
    assert isinstance(saved.verdicts[0], SendBackVerdict)
    worker = comms.registry.require('worker')
    assert why in GoalSegment.capture(worker, worker.goal, comms.goals).progress
    assert judge.Edit.unjudged(comms, 'lead') == []
    audience = SendBackVerdict.audience.of('worker', saved.goal)
    assert [to for to, _ in posted] == [audience, audience]
    assert not (script.state / 'verdicts.jsonl').exists()


@pytest.mark.parametrize('kind', [AcceptedVerdict, SendBackVerdict])
def test_each_verdict_kind_reaches_its_unit_and_the_threads_whose_goal_the_order_serves(commander, kind):
    comms, _ = commander
    todo = create(commander, 'P1')
    assert kind.audience.of('worker', todo.goal).threads == ('worker', 'lead')
    own = GoalRef('worker', comms.registry.require('worker').created_at, 'goal')
    assert kind.audience.of('worker', own).threads == ('worker',)


def test_each_send_target_preserves_collective_body_and_prefixes_named_audience(commander):
    comms, caller = commander
    channel = CommandTarget.named('#all')
    assert channel.addressed((), 'Read the order') == 'Read the order'
    assert channel.addressed(('worker', 'lead'), 'Read the order') == '@worker @lead Read the order'
    for target, audience in (('worker', ()), ('#all', ()), ('#all', ('worker',)),
                             ('#all', ('worker', 'lead'))):
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
        CommsVerdictTool(problem='P1', operation=AcceptedVerdict(
            todo.revision, lead, todo.assignment, 'toolu_1', quote='Delete the whole bus now')).apply(comms, caller)
    assert comms.goals.todos.get('P1') == todo
    accepted = AcceptedVerdict(todo.revision, lead, todo.assignment, 'toolu_1', quote='Repair  the existing owner')
    CommsVerdictTool(problem='P1', operation=accepted).apply(comms, caller)
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
    CommsVerdictTool(problem='P1', operation=sent_back).apply(comms, caller)
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
    CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    why = 'The unit kept a second copy of the goal text.'
    CommsVerdictTool(problem='P2', operation=SendBackVerdict(todo.revision, comms.registry.require('lead').incarnation,
        todo.assignment, 'toolu_3', reason=StrikeReason(why))).apply(comms, caller)
    worker = comms.registry.require('worker')
    progress = GoalSegment.capture(worker, worker.goal, comms.goals).progress
    assert progress.splitlines() == ['P1: Delivered: operation checked, waiting on install', f'P2: Open · Sent back: {why}']
    assert (worker.goal.text, worker.goal.progress) == (ProblemGoalOrder.text, '')
    with pytest.raises(ValueError, match='block the problem.s Todo, or standby with wait_for'):
        comms.goals.update_goal('worker', BlockedGoalAction(block_reason='Waiting on lead.'), actor=ModelInvocable)
    assert comms.registry.require('worker').goal == worker.goal

VERDICT_LINES = {
    'ok': "e1:ab --msg m1 --line one --why why --serves 'Repair the existing owner'",
    'superseded': "e1:ab --line one --why 'The task moved to a different implementation owner' --by m2",
    'back': "e1:ab --msg m1 --line one --why 'The unit wrapped the old store' --roast r --fix f --slop IDEN-5"
            " --fact 'four words of fact' --at a.py:1 --derive delete-copy --by b.py:2",
}


def test_each_verdict_kind_requires_its_facts_and_records_its_verdict(commander, commander_script):
    """Every verdict member refuses a line missing any fact it declares, and its verdict reads back from the order."""
    script, _ = commander_script
    comms, caller = commander
    judge = importlib.reload(importlib.import_module('agent_comms.commander.judge'))
    assert set(judge.VerdictVerb.names()) == set(VERDICT_LINES)
    create(commander, 'P1')
    comms.registry.declare(comms.registry.new_thread('replacement', frozenset(), str(comms.root), created_at=3.0))
    replacing = create(commander, 'P2', owner='replacement')
    CommsChangeProblemTool(problem='P2', operation=OrderMessage(replacing.revision,
        comms.registry.require('lead').incarnation, 'm2')).apply(comms, caller)
    for name, line in VERDICT_LINES.items():
        argv = [name, *shlex.split(line)]
        for at in [i for i, token in enumerate(argv)
                   if token.startswith('--') and (token != '--msg' or name == 'back')]:
            with pytest.raises(SystemExit):
                judge.JudgeVerb.parse(argv[:at] + argv[at + 2:], '')
        command = judge.JudgeVerb.parse(argv, '')
        object.__setattr__(command, 'context', script)
        operation = command.operation(script, comms.goals.todos.get('P1'), f'toolu_{name}')
        CommsVerdictTool(problem='P1', operation=operation).apply(comms, caller)
        assert comms.goals.todos.get('P1').verdicts[-1] == operation


def test_close_quotes_the_works_line(commander):
    """Closing evidence must quote four consecutive words of the order's Works when run."""
    comms, caller = commander
    run = ApplicationOrderRun('The board shows the problem closed after the commander reopens the session')
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem='P1', order=replace(order(comms.root, 'order-P1'), run=run), request='request',
        deadline=RecordedEventTime(1000), commander='lead', repo='OpenHCSDev/agent-comms', text='Fix P1',
    )).apply(comms, caller)
    CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
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
    deletes = FieldCodec.decode(OrderDeletion, {
        'kind': 'family', 'family': 'is_set absence decision',
        'site': f'{copied / "flag.py"}:1',
        'count': {'directory': str(copied), 'command': family,
                  'result': {'kind': 'count', 'expected': 0}}})
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
    with pytest.raises(ValueError, match=r'The order check is 1 away from CountCheckResult\(expected=0\)\.'):
        CommsOrderDoneTool(problem='P520', order_id=comms.goals.todos.get('P520').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert comms.goals.todos.get('P520') == pending
    (copied / 'flag.py').unlink()
    callers.write_text('def read(v):\n    return int(v is not None)\n')
    CommsOrderDoneTool(problem='P520', order_id=comms.goals.todos.get('P520').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
    assert not comms.goals.todos.get('P520').pending


def test_send_guard_refuses_a_raw_send_only_to_the_commanders_units(commander, commander_script, monkeypatch, capsys):
    """worker holds one of lead's problems, so it is reached through orders; the owner, claude-ai
    and a peer commander hold none and are messaged directly."""
    script, _ = commander_script
    import io
    from agent_comms.commander import send_guard
    create(commander, 'P1')

    def guarded(*targets):
        command = 'agent-comms send --from lead ' + ' '.join(f'--to {target}' for target in targets) + ' --body hi'
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'session_id': 's', 'hook_event_name': 'PreToolUse', 'tool_input': {'command': command}})))
        send_guard.main([], script)
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
    it is refused with the order to send worker, and once it is planned for lead it runs. The same command from another session, and an
    order whose body names the command, pass. Once lead's process is gone no session can be
    classified, so every session is refused with the read error."""
    script, _ = commander_script
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
            hook = {'session_id': session, 'hook_event_name': 'PreToolUse', 'tool_input': {'command': command}}
            monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(hook)))
            send_guard.main([], script)
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
        PlannedInstallDeadline(RecordedEventTime(2000.0), 'lead').plan(comms.root)
        assert guarded(command, 's1') == ''
    finally:
        process.kill()
        process.wait()
    unclassified = json.loads(guarded(command, 's2'))['hookSpecificOutput']['permissionDecisionReason']
    assert f'{kind.capitalize()} refused: the guard cannot tell whether this session is lead' in unclassified
    assert f'/proc/{process.pid}/environ' in unclassified


def test_a_send_quoted_in_a_message_body_is_not_a_raw_send(commander, commander_script, monkeypatch, capsys):
    script, _ = commander_script
    import io
    from agent_comms.commander import send_guard
    create(commander, 'P1')
    for command in ('agent-comms send --from user --to lead --body "agent-comms send --from lead --to worker"',
                    'agent-comms send --from user --to lead --body "x; agent-comms send --from lead --to worker"'):
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'session_id': 's', 'hook_event_name': 'PreToolUse', 'tool_input': {'command': command}})))
        send_guard.main([], script)
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
        CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)

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
            CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=OperationDelivery(), claims=(claim,)).apply(comms, caller)
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
        (CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=OperationDelivery(), claims=()), 'A delivered report needs a claim'),
        (CommsGoalTool(goal_id='request', status=CompletedGoalAction, progress='done'), 'A completed report needs a claim'),
    ):
        with pytest.raises(ValueError, match=re.escape(refused)):
            report.apply(comms, caller)
    CommsOrderDoneTool(problem='P1', order_id=comms.goals.todos.get('P1').order.id, delivery=OperationDelivery(), claims=merged).apply(comms, caller)
    assert isinstance(comms.goals.todos.get('P1').order, DeliveredProblemOrder)
    report = CommsGoalTool(goal_id='request', status=CompletedGoalAction, progress='done', claims=merged)
    assert isinstance(report.apply(comms, caller).goal.state, CompletedGoal)
