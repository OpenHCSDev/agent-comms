"""Commander forks register once, own real orders, and cannot share a unit."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from source_revision_fixture import SourceRepository
from agent_comms.commander.order import Commander, OrderFile
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck, RequestGoalOrder
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_model import DeclaredModel
from agent_comms.todos import GoalRef, PendingProblemOrder, TodoError, TodoStore, UnitAssignment


@pytest.fixture
def fork_bus(tmp_path):
    source = SourceRepository.create(tmp_path / 'source')
    source.git(source.checkout, 'remote', 'set-url', 'origin', 'https://github.com/example/source.git')
    comms = Comms(tmp_path / 'bus')
    comms.messaging.initialize_private_initial_protocol()
    comms.channels.create_tag('comms')
    for name in ('lead', 'worker', 'other'):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset({'comms'}), str(source.checkout),
            model=DeclaredModel('anthropic/claude-opus-4-6'), execution=ExternalThreadExecution))
    comms.goals.update_goal('lead', SetGoalAction(
        text='Repair the commander workflow', goal_id='request', order=RequestGoalOrder()))
    config = Commander.on_machine('lead', (source,), comms.root, tmp_path)
    config.root_path(comms.root).write_text(config.model_dump_json())
    now = time.time()
    order = PendingProblemOrder(
        id='first-order', owner=source.scope().shape.parents[0], scope=source.scope(),
        run=MechanicalOrderRun(), check=OrderCheck(str(source.checkout), 'printf 0', CountCheckResult(1)),
        edits=('Repair the existing declaration and remove the duplicate decision.',),
        banned=('No second assignment store.',), report='Report the working command.',
        at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 600))
    # This is the retained parent-owned namespace. Forks must allocate beyond it.
    lead = comms.registry.require('lead')
    comms.goals.todos.create('P40', source.github, 'Existing parent problem', creator=lead,
        owner=comms.registry.require('other'), generation='retained',
        review_at=RecordedEventTime(now + 600), order=replace(order, id='retained-order'),
        goal=GoalRef('lead', lead.created_at, 'request'))
    return comms, source, order


def command(comms, *args):
    return subprocess.run(
        [sys.executable, '-c', 'from agent_comms.commander.cli import main; main()',
         '--root', str(comms.root), *args],
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')},
        capture_output=True, text=True, timeout=40)


def document(path, order):
    path.write_text(json.dumps(FieldCodec.encode(OrderFile(
        order=order,
        defect='Two commander forks can assign conflicting work to the same unit.',
        why_stupid='This is stupid because assignment ownership is not recorded anywhere.',
        unplanned_reason='This failure blocks the commander from assigning independent work safely.'))))
    return str(path)


def test_fork_orders_refuse_competitors_and_closing_report_releases(fork_bus, tmp_path):
    comms, source, order = fork_bus
    first = document(tmp_path / 'first.json', order)
    result = command(comms, 'order', 'problem', first, '--to', 'worker',
                     '--where', 'owner.py:2', '--fork', 'fork-one')
    assert result.returncode == 0, result.stderr
    fork = comms.registry.require('fork-one')
    assert fork.parent.named('lead')
    held = comms.goals.todos.held_for_commander(fork.incarnation)
    assert [todo.id for todo in held] == ['P41']
    second = document(tmp_path / 'second.json', replace(order, id='second-order'))
    for prefix in (('--fork', 'fork-two'), ()):
        result = command(comms, 'order', 'problem', second, '--to', 'worker',
                         '--where', 'owner.py:2', *prefix)
        assert result.returncode != 0
        assert 'worker is being assigned by fork-one on open order first-order' in result.stderr
    # The second live fork can assign independent work and must appear beside the first.
    result = command(comms, 'order', 'problem', second, '--to', 'other',
                     '--where', 'owner.py:2', '--fork', 'fork-two')
    assert result.returncode == 0, result.stderr
    result = command(comms, 'capacity')
    assert result.returncode == 0, result.stderr
    assert "2 live forks ['fork-one', 'fork-two']" in result.stdout
    # Send shares the same owner check; neither existing nor native orders bypass it.
    resend = replace(OrderFile.read(Path(first).read_text()), problems=('P41',))
    Path(first).write_text(json.dumps(FieldCodec.encode(resend)))
    result = command(comms, 'order', 'send', 'worker', first, '--fork', 'fork-two')
    assert result.returncode != 0 and 'fork-one on open order first-order' in result.stderr
    with pytest.raises(TodoError, match='fork-one on open order first-order'):
        comms.goals.todos.take_unit(comms.registry.require('worker').incarnation,
                                   comms.registry.require('lead').incarnation, 'competing')
    closing = tmp_path / 'closing.txt'
    closing.write_text('The assigned work is complete; this fork is closing.')
    result = command(comms, 'order', 'say', '#comms', str(closing), '--fork', 'fork-one')
    assert result.returncode == 0, result.stderr
    assert not comms.registry.status('fork-one').receiving
    with comms.goals.todos.snapshot() as db:
        assert not UnitAssignment.select(db, assigner=fork.incarnation)
    assert "1 live forks ['fork-two']" in command(comms, 'capacity').stdout
    assert comms.goals.todos.get('P41').holder == 'worker'
    assert comms.goals.todos.get('P40').creator == 'lead'
    # Retirement never permits a later CLI invocation to resurrect the same fork.
    result = command(comms, 'order', 'board', '--fork', 'fork-one')
    assert result.returncode != 0 and 'has retired' in result.stderr


def test_two_commanders_allocate_distinct_global_problem_ids(fork_bus, tmp_path):
    comms, source, order = fork_bus
    def assign(name, unit):
        body = document(tmp_path / f'{name}.json', replace(order, id=name))
        return command(comms, 'order', 'problem', body, '--to', unit,
                       '--where', 'owner.py:2', '--fork', name)
    with ThreadPoolExecutor(max_workers=2) as commands:
        results = tuple(commands.map(lambda pair: assign(*pair),
                                     (('fork-a', 'worker'), ('fork-b', 'other'))))
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    todos = tuple(todo for name in ('fork-a', 'fork-b')
                  for todo in comms.goals.todos.held_for_commander(comms.registry.require(name).incarnation))
    assert {todo.id for todo in todos} == {'P41', 'P42'}
    assert {todo.creator for todo in todos} == {'fork-a', 'fork-b'}
