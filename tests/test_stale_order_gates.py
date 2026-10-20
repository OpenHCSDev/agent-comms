"""Real TodoStore rows exercise the gate family and merged-delivery observer."""
import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.commander.order import OrderFile, OrderVerb, StaleOrderGate
from agent_comms.plan_phases import PlanPhase, RecordPlanePhase, UnplannedPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.todos import AssignTodo, CommitDelivery, OperationDelivery, WithdrawTodo
from agent_comms.tools import CommsOrderDoneTool
from test_commander_problem_orders import create, order_body, working


def test_stale_gate_covers_its_verb_family_but_allows_board_repairs(
        commander, commander_script, tmp_path):
    comms, _ = commander
    context, _ = commander_script
    todo = create(commander, 'P244', deadline=time.time() - 1900)
    body = str(tmp_path / 'must-not-be-read')
    blocked = (
        ['send', 'worker', '23:59', body], ['say', 'worker', body],
        ['problem', body, '--to', 'worker', '--due', '23:59', '--where', f'{__file__}:1'],
        ['strike', 'worker', '--why', 'P244 missed its deadline'],
    )
    verbs = tuple(OrderVerb.parse(args, '') for args in blocked)
    assert {type(verb) for verb in verbs} == set(OrderVerb.members_with(StaleOrderGate))
    for verb in verbs:
        with pytest.raises(SystemExit) as refused:
            verb.apply(context)
        text = str(refused.value)
        assert 'P244: holder worker, deadline ' in text
        assert 'more than 30 minutes overdue' in text
    why = 'The current source needs one more bounded check before delivery.'
    repairs = (
        ['due', 'P244', '23:59', '--why', why], ['withdraw', 'P244', '--why', why],
        ['done', 'worker', 'order-P244', '--sha', 'a' * 40],
        ['close', 'P244', '--works', why],
        ['check', 'P244', '--check', f'{tmp_path} :: printf 0 -> 0', '--why', why],
        ['serve', 'P244', 'record_plane'], ['drop', 'worker', 'old', '--by', 'new'],
    )
    for args in repairs:
        OrderVerb.parse(args, '').gate(context)
    actor = comms.registry.require('lead').incarnation
    comms.goals.todos.apply(todo.id, AssignTodo(
        todo.revision, todo.held(), RecordedEventTime(time.time() + 3600), todo.order, todo.goal))
    verbs[0].gate(context)
    moved = comms.goals.todos.get(todo.id)
    comms.goals.todos.apply(todo.id, WithdrawTodo(
        moved.revision, actor, moved.held(), why, None))
    verbs[0].gate(context)
    assert not comms.goals.todos.get(todo.id).state.open


@pytest.mark.parametrize('phase', (UnplannedPhase, *PlanPhase.members_with(PlanPhase)))
def test_phase_gate_uses_the_phase_family_and_requires_an_explicit_unplanned_reason(
        commander, commander_script, phase):
    comms, _ = commander
    context, repo = commander_script
    (repo / 'count.txt').write_text('0\n')
    payload = json.loads(Path(order_body(repo)).read_text())
    payload['order']['serves'] = phase.declared_name
    if phase is UnplannedPhase:
        del payload['unplanned_reason']
        with pytest.raises(SystemExit, match='Plan phases are ready'):
            context.send('worker', '23:59', OrderFile.read(json.dumps(payload)),
                         clean=True, new=('P1', 'Repair the owner'))
        assert comms.goals.todos.for_commander(comms.registry.require('lead').incarnation) == ()
        payload['unplanned_reason'] = (
            'This urgent retained incident is outside the declared plan dependency chain.')
    context.send('worker', '23:59', OrderFile.read(json.dumps(payload)),
                 clean=True, new=('P1', 'Repair the owner'))
    assert comms.goals.todos.get('P1').order.serves is phase


def test_second_plan_order_gate_names_the_busy_order_and_running_unowned_units(
        commander, commander_script):
    comms, _ = commander
    context, repo = commander_script
    todo = create(commander, 'P1')
    with comms.goals.todos.writing() as db:
        replace(todo, order=replace(todo.order, serves=RecordPlanePhase)).upsert(db)
    comms.registry.declare(comms.registry.new_thread('free-unit', frozenset(), str(repo)))
    comms.owners.acquire_thread('free-unit', owner_pid=os.getpid())
    (repo / 'count.txt').write_text('0\n')
    payload = json.loads(Path(order_body(repo)).read_text())
    payload['order']['serves'] = RecordPlanePhase.declared_name
    with pytest.raises(SystemExit) as refused:
        context.send('worker', '23:59', OrderFile.read(json.dumps(payload)),
                     clean=True, new=('P2', 'Repair another owner'))
    assert 'worker already holds plan orders P1' in str(refused.value)
    assert 'running units holding no order: free-unit' in str(refused.value)
    assert [todo.id for todo in comms.goals.todos.for_commander(
        comms.registry.require('lead').incarnation)] == ['P1']


@pytest.mark.parametrize('commit_delivery', (True, False))
def test_observer_closes_only_delivered_commits_present_in_fetched_main(
        commander, tmp_path, commit_delivery):
    comms, caller = commander
    root = comms.root
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), '-c', 'user.name=t',
            '-c', 'user.email=t@t', *args], text=True).strip()
    git('init', '-q', '-b', 'main')
    git('remote', 'add', 'origin', str(origin))
    git('commit', '-q', '--allow-empty', '-m', 'delivered source')
    sha = git('rev-parse', 'HEAD')
    git('push', '-q', 'origin', 'HEAD:main')
    todo = create(commander, 'P1', deadline=time.time() + 3600)
    with comms.goals.todos.writing() as db:
        replace(todo, order=replace(todo.order, serves=RecordPlanePhase)).upsert(db)
    delivery = (CommitDelivery(commit=sha, delivered_commit=sha) if commit_delivery
                else OperationDelivery())
    CommsOrderDoneTool(problem='P1', order_id=todo.order.id, delivery=delivery,
                      claims=working(comms)).apply(comms, caller)
    assert comms.goals.todos.get('P1').state.open
    repo = Repo(checkout=root, github=todo.repo)
    actor = comms.registry.require('lead').incarnation
    comms.goals.todos.reconcile_merges(actor, (repo,),
                                    comms.registry.storage_rule)
    observed = comms.goals.todos.get('P1')
    assert observed.state.open is not commit_delivery
    assert observed.order.serves is RecordPlanePhase
    comms.goals.todos.reconcile_merges(actor, (repo,),
                                    comms.registry.storage_rule)
    assert comms.goals.todos.get('P1') == observed
