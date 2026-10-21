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
from agent_comms.change_report import FileLine
from agent_comms.commander.order import Commander, OrderFile
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.errors import RelationViolationError
from agent_comms.goal_actions import SetGoalAction
from agent_comms.installer.backup import StoreBackup
from agent_comms.goals import CountCheckResult, OrderCheck, RequestGoalOrder
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_model import DeclaredModel
from agent_comms.todos import (
    DeclarationDerivation, GoalRef, PendingProblemOrder, ProblemNumber, Slop, TodoError,
    TodoStore, UnitAssignment,
)


@pytest.fixture
def fork_bus(tmp_path):
    source = SourceRepository.create(tmp_path / 'source')
    source.git(source.checkout, 'checkout', 'HEAD', '--', 'owner.py')
    (source.checkout / 'declaration.py').write_text('class AssignmentOwner:\n    pass\n')
    source.git(source.checkout, 'add', 'declaration.py')
    source.git(source.checkout, 'commit', '-qm', 'Declare shared assignment owner')
    source.git(source.checkout, 'push', 'origin', 'main')
    comms = Comms(tmp_path / 'saved-bus')
    comms.messaging.initialize_private_initial_protocol()
    comms.channels.create_tag('comms')
    for name in ('lead', 'worker', 'other'):
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
    # This is the retained parent-owned namespace. Forks must allocate beyond it.
    lead = comms.registry.require('lead')
    comms.goals.todos.create('P40', source.github, 'Existing parent problem', creator=lead,
        owner=comms.registry.require('other'), generation='retained',
        review_at=RecordedEventTime(now + 600), order=replace(order, id='retained-order'),
        goal=GoalRef('lead', lead.created_at, 'request'))
    copied = tmp_path / 'copied-bus'
    StoreBackup.copy_tree(comms.root, copied)
    return Comms(copied), source, order


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
        why_stupid='This is stupid because the assigning fork is not recorded anywhere.',
        unplanned_reason='This failure blocks the commander from assigning independent work safely.',
        slop=(Slop('IDEN-5', 'which fork is assigning this unit', FileLine('owner.py', 2),
                   DeclarationDerivation, FileLine('declaration.py', 1)),)))))
    return str(path)


def test_fork_orders_refuse_competitors_and_closing_report_releases(fork_bus, tmp_path):
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
    second = document(tmp_path / 'second.json', replace(order, id='second-order'))
    for prefix in (('--fork', 'fork-two'), ()):
        result = command(comms, 'order', 'problem', second, '--to', 'worker',
                         '--where', 'owner.py:2', *prefix)
        assert result.returncode != 0
        assert 'worker is being assigned by fork-one on open order first-order' in result.stderr
        print(result.stderr.strip())
    # The second live fork can assign independent work and must appear beside the first.
    result = command(comms, 'order', 'problem', second, '--to', 'other',
                     '--where', 'owner.py:2', '--fork', 'fork-two')
    assert result.returncode == 0, result.stderr
    result = command(comms, 'capacity')
    assert result.returncode == 0, result.stderr
    assert "2 live forks ['fork-one', 'fork-two']" in result.stdout
    print(result.stdout.strip())
    # Send shares the same owner check; neither existing nor native orders bypass it.
    resend = replace(OrderFile.read(Path(first).read_text()), problems=('P41',))
    Path(first).write_text(json.dumps(FieldCodec.encode(resend)))
    result = command(comms, 'order', 'send', 'worker', first, '--fork', 'fork-two')
    assert result.returncode != 0 and 'fork-one on open order first-order' in result.stderr
    lead = comms.registry.require('lead')
    worker = comms.registry.require('worker')
    retained = comms.goals.todos.get('P40')
    # Native create and assign share the store check, not just the CLI preflight.
    with pytest.raises(TodoError, match='fork-one on open order first-order'):
        comms.goals.todos.create('P999', source.github, 'Conflicting native order',
            creator=lead, owner=worker, generation='competing', goal=retained.goal,
            review_at=retained.review_at, order=order)
    with pytest.raises(TodoError, match='fork-one on open order first-order'):
        comms.goals.todos.assign('P40', expected_revision=retained.revision,
            owner=worker, assigner=lead.incarnation, generation='competing',
            goal=retained.goal, review_at=retained.review_at, order=order)
    closing = tmp_path / 'closing.txt'
    closing.write_text('The assigned work is complete; this fork is closing.')
    result = command(comms, 'order', 'say', '#comms', str(closing), '--fork', 'fork-one')
    assert result.returncode == 0, result.stderr
    assert not comms.registry.status('fork-one').receiving
    with comms.goals.todos.snapshot() as db:
        assert not UnitAssignment.select(db, assigner=fork.incarnation)
    with pytest.raises(RelationViolationError, match='stopped or unavailable'):
        comms.goals.todos.take_unit(worker.incarnation, fork.incarnation, 'late-order')
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
    print('Concurrent fork allocations:', sorted((todo.creator, todo.id) for todo in todos))


def test_retained_store_carry_preserves_orders_and_seeds_global_numbers(fork_bus):
    from cutover.carry_unit_assignments import UnitAssignments

    comms, source, order = fork_bus
    store = comms.goals.todos
    retained = store.get('P40')
    # The old store has precisely the same Todo/Hunt schema, without the new tables.
    with store.writing() as db:
        for row in (UnitAssignment, ProblemNumber):
            db.execute(f'DROP TABLE "{row.declared_name}"')
    assert UnitAssignments.carry(comms.root, apply=False).changed == 2
    assert UnitAssignments.carry(comms.root, apply=True).changed == 2
    assert store.get('P40') == retained
    assert store.allocate_problem() == 'P41'
    assert UnitAssignments.carry(comms.root, apply=True).changed == 0
