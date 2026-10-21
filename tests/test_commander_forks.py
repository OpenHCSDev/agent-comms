"""Commander forks use registered identity and the original Todo assignment."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from source_revision_fixture import SourceRepository
from agent_comms.change_report import FileLine
from agent_comms.commander.cli import main
from agent_comms.commander.order import Commander, OrderFile
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.installer.backup import StoreBackup
from agent_comms.goals import CountCheckResult, OrderCheck, RequestGoalOrder
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_model import DeclaredModel
from agent_comms.todos import (
    DeclarationDerivation, GoalRef, PendingProblemOrder, ProblemNumber, ReleaseTodo,
    Slop, StrikeReason, Todo, TodoError,
)


@pytest.fixture
def fork_bus(tmp_path):
    source = SourceRepository.create(tmp_path / 'source')
    (source.checkout / 'declaration.py').write_text('class AssignmentOwner:\n    pass\n')
    source.git(source.checkout, 'add', 'declaration.py')
    source.git(source.checkout, 'commit', '-qm', 'Declare shared assignment owner')
    source.git(source.checkout, 'push', 'origin', 'main')
    comms = Comms(tmp_path / 'saved-bus')
    comms.messaging.initialize_private_initial_protocol()
    comms.channels.create_tag('comms')
    for name in ('lead', 'worker', 'other', 'retained'):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset({'comms'}), str(source.checkout),
            model=DeclaredModel('anthropic/claude-opus-4-6'), execution=ExternalThreadExecution))
    comms.goals.update_goal('lead', SetGoalAction(
        text='Repair the commander workflow', goal_id='request', order=RequestGoalOrder()))
    core = Repo.of(Path(__file__).parents[1])
    config = Commander.on_machine('lead', (source, core), comms.root, tmp_path)
    config.root_path(comms.root).write_text(config.model_dump_json())
    now = time.time()
    order = PendingProblemOrder(
        id='first-order', owner=source.scope().shape.parents[0], scope=source.scope(),
        run=MechanicalOrderRun(), check=OrderCheck(str(source.checkout), 'printf 0', CountCheckResult(1)),
        edits=('Repair the existing declaration and remove the duplicate decision.',),
        banned=('No second assignment store.',), report='Report the working command.',
        at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 600))
    lead = comms.registry.require('lead')
    comms.goals.todos.create('P40', source.github, 'Existing parent problem', creator=lead,
        owner=comms.registry.require('retained'), generation='retained',
        review_at=RecordedEventTime(now + 600), order=replace(order, id='retained-order'),
        goal=GoalRef('lead', lead.created_at, 'request'))
    copied = tmp_path / 'copied-bus'
    StoreBackup.copy_tree(comms.root, copied)
    return Comms(copied), source, order


def command(comms, *args):
    """Exercise the real parser and state without cold-importing Core for every verb."""
    argv = ['agent-comms-commander', '--root', str(comms.root), *args]
    output, error = StringIO(), StringIO()
    code = 0
    with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(error):
        try:
            main()
        except SystemExit as exited:
            # CPython prints a non-integer SystemExit value and returns 1.
            if isinstance(exited.code, str):
                print(exited.code, file=error)
                code = 1
            else:
                code = exited.code or 0
    return subprocess.CompletedProcess(argv, code, output.getvalue(), error.getvalue())


def process_command(comms, *args):
    """Independent processes must resolve identity and acquire the same Todo write lock."""
    return subprocess.run(
        [sys.executable, '-c', 'from agent_comms.commander.cli import main; main()',
         '--root', str(comms.root), *args],
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')},
        capture_output=True, text=True, timeout=90)


def document(path, order):
    path.write_text(json.dumps(FieldCodec.encode(OrderFile(
        order=order,
        defect='Two commander forks can assign conflicting work to the same unit.',
        why_stupid='This is stupid because the original assignment is not checked.',
        unplanned_reason='This failure blocks the commander from assigning independent work safely.',
        slop=(Slop('IDEN-5', 'which fork is assigning this unit', FileLine('owner.py', 2),
                   DeclarationDerivation, FileLine('declaration.py', 1)),)))))
    return str(path)


@pytest.mark.timeout(180)
def test_fork_orders_refuse_from_todos_and_retirement_is_explicit(fork_bus, tmp_path):
    comms, source, order = fork_bus
    first = document(tmp_path / 'first.json', order)
    result = command(comms, 'order', 'problem', first, '--to', 'worker',
                     '--where', 'owner.py:2', '--fork', 'fork-one')
    assert result.returncode == 0, result.stderr
    fork = comms.registry.require('fork-one')
    assert fork.parent.named('lead')
    assert fork.goal.require().id != comms.registry.require('lead').goal.require().id
    held = comms.goals.todos.held_for_commander(fork.incarnation)
    assert [todo.id for todo in held] == ['P41']
    original = held[0]
    second = document(tmp_path / 'second.json', replace(order, id='second-order'))
    refusal = 'worker is assigned by fork-one on open order first-order (P41)'
    for prefix in (('--fork', 'fork-two'), ()):
        result = command(comms, 'order', 'problem', second, '--to', 'worker',
                         '--where', 'owner.py:2', *prefix)
        assert result.returncode != 0 and refusal in result.stderr
        print(result.stderr.strip())
    result = command(comms, 'order', 'problem', second, '--to', 'other',
                     '--where', 'owner.py:2', '--fork', 'fork-two')
    assert result.returncode == 0, result.stderr
    result = command(comms, 'capacity')
    assert result.returncode == 0 and "2 live forks ['fork-one', 'fork-two']" in result.stdout
    print(result.stdout.strip())
    second_owned = comms.goals.todos.held_for_commander(
        comms.registry.require('fork-two').incarnation)[0]
    # Reassign this fork's own problem, not the other commander's P number.
    resend = replace(OrderFile.read(Path(second).read_text()), problems=(second_owned.id,))
    resend_file = tmp_path / 'reassign.json'
    resend_file.write_text(json.dumps(FieldCodec.encode(resend)))
    result = command(comms, 'order', 'send', 'worker', str(resend_file),
        '--fork', 'fork-two', '--reassign',
        'The copied fixture exercises transfer after the first worker failed its assigned review.')
    assert result.returncode != 0 and refusal in result.stderr
    assert comms.goals.todos.get(second_owned.id) == second_owned
    lead = comms.registry.require('lead')
    worker = comms.registry.require('worker')
    retained = comms.goals.todos.get('P40')
    with pytest.raises(TodoError, match='fork-one on open order first-order'):
        comms.goals.todos.create('P999', source.github, 'Conflicting native order',
            creator=lead, owner=worker, generation='competing', goal=retained.goal,
            review_at=retained.review_at, order=order)
    with pytest.raises(TodoError, match='fork-one on open order first-order'):
        comms.goals.todos.assign('P40', expected_revision=retained.revision,
            owner=worker, assigner=lead.incarnation, generation='competing',
            goal=retained.goal, review_at=retained.review_at, order=order)
    interim = tmp_path / 'interim.txt'
    interim.write_text('The first review is complete; another check remains.')
    result = command(comms, 'order', 'say', '#comms', str(interim), '--fork', 'fork-one')
    assert result.returncode == 0, result.stderr
    assert comms.registry.status('fork-one').receiving
    assert comms.goals.todos.get('P41') == original
    result = command(comms, 'order', 'retire', '--fork', 'fork-one')
    assert result.returncode == 0, result.stderr
    assert not comms.registry.status('fork-one').receiving
    assert comms.goals.todos.get('P41') == original
    assert "1 live forks ['fork-two']" in command(comms, 'capacity').stdout
    # Stopping a thread cannot discard its open work. Only the original Todo changes it.
    result = command(comms, 'order', 'problem', second, '--to', 'worker',
                     '--where', 'owner.py:2')
    assert result.returncode != 0 and refusal in result.stderr
    comms.goals.todos.apply('P41', ReleaseTodo(
        original.revision, fork.incarnation, original.held(),
        StrikeReason('The fixture releases its actual original order after the completed review.')))
    result = command(comms, 'order', 'problem', second, '--to', 'worker',
                     '--where', 'owner.py:2')
    assert result.returncode == 0, result.stderr
    assert comms.goals.todos.get('P40') == retained


def test_two_commanders_allocate_distinct_global_problem_ids(fork_bus, tmp_path):
    comms, source, order = fork_bus
    def assign(pair):
        name, unit = pair
        body = document(tmp_path / f'{name}.json', replace(order, id=name))
        return process_command(comms, 'order', 'problem', body, '--to', unit,
                       '--where', 'owner.py:2', '--fork', name)
    with ThreadPoolExecutor(max_workers=2) as commands:
        results = tuple(commands.map(assign, (('fork-a', 'worker'), ('fork-b', 'other'))))
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    todos = tuple(todo for name in ('fork-a', 'fork-b')
                  for todo in comms.goals.todos.held_for_commander(comms.registry.require(name).incarnation))
    assert {todo.id for todo in todos} == {'P41', 'P42'}
    assert {todo.creator for todo in todos} == {'fork-a', 'fork-b'}
    print('Concurrent fork allocations:', sorted((todo.creator, todo.id) for todo in todos))


def test_two_commanders_cannot_assign_the_same_unit_concurrently(fork_bus, tmp_path):
    comms, source, order = fork_bus
    def assign(name):
        body = document(tmp_path / f'{name}.json', replace(order, id=name))
        return process_command(comms, 'order', 'problem', body, '--to', 'worker',
                       '--where', 'owner.py:2', '--fork', name)
    with ThreadPoolExecutor(max_workers=2) as commands:
        results = tuple(commands.map(assign, ('fork-a', 'fork-b')))
    assert sum(result.returncode == 0 for result in results) == 1, [r.stderr for r in results]
    held = tuple(todo for name in ('fork-a', 'fork-b')
                 for todo in comms.goals.todos.held_for_commander(comms.registry.require(name).incarnation))
    assert len(held) == 1 and held[0].holder == 'worker'
    refused = next(result for result in results if result.returncode)
    assert f'is assigned by {held[0].creator}' in refused.stderr
    print(refused.stderr.strip())


def test_retained_store_carry_preserves_orders_and_seeds_global_numbers(fork_bus, tmp_path):
    from cutover.carry_problem_numbers import ProblemNumbers

    comms, source, order = fork_bus
    store = comms.goals.todos
    retained = store.get('P40')
    with store.writing() as db:
        db.execute(f'DROP TABLE "{ProblemNumber.declared_name}"')
        db.execute(f'DROP INDEX "{Todo.unit_index.name(Todo)}"')
    assert ProblemNumbers(comms.root, comms.root, tmp_path, False).carry().changed == 2
    assert ProblemNumbers(comms.root, comms.root, tmp_path, True).carry().changed == 2
    assert store.get('P40') == retained
    assert store.allocate_problem() == 'P41'
    assert ProblemNumbers(comms.root, comms.root, tmp_path, True).carry().changed == 0
