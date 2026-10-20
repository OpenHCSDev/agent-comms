"""Measured order timing uses retained deliveries and the real commander entrypoint."""
import json
import math
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import DoneTodoState, MeasuredDeadline, OperationDelivery, TodoStore
from test_commander_problem_orders import create, order, order_body


def test_measured_deadline_from_delivered_orders(commander, commander_script):
    context, repo = commander_script
    store = context.comms.goals.todos
    completed = []
    # 30, 120 and 150 seconds per edit: the median is 120, not total time / total edits.
    for number, edits, elapsed in ((10, 2, 60), (11, 3, 360), (12, 2, 300)):
        todo = create(commander, f'P{number}')
        pending = replace(todo.order, edits=tuple(f'Repair owner {i}' for i in range(edits)))
        delivery = OperationDelivery(at=RecordedEventTime(100 + elapsed))
        delivered = pending.delivered(delivery)
        completed.append(DoneTodoState.closed(todo, delivered.installed('Confirmed the operation', 'receipt.txt')))
    # A linked problem can retain the same delivered order; it remains one observation.
    completed[1] = replace(completed[1], prior_orders=(completed[0].order,))
    with store.writing() as db:
        for todo in completed:
            todo.upsert(db)
    measured = MeasuredDeadline.read(store)
    assert measured.seconds_per_edit == 120
    assert len(measured.rates) == 3

    (repo / 'count.txt').write_text('0\n')
    path = Path(order_body(repo))
    document = json.loads(path.read_text())
    document['order']['edits'] = [
        'Set count.txt to 1.', 'Remove duplicate count writers.', 'Report the owner change.',
    ]
    path.write_text(json.dumps(document))
    cli = [str(Path(sys.executable).with_name('agent-comms-commander')),
           '--root', str(context.comms.root), 'order']
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')}

    def run(*args):
        result = subprocess.run([*cli, *args], capture_output=True, text=True, timeout=60, env=env)
        assert result.returncode == 0, result.stderr
        return result

    created = run('problem', str(path), '--to', 'worker', '--where', f'{__file__}:1')
    current = store.get('P13')
    assert current.deadline.at == math.ceil((current.order.at.at + 360) / 60) * 60
    document['problems'] = ['P13']
    path.write_text(json.dumps(document))
    sent = run('send', 'worker', str(path), '--clean', '--replaces', current.messages[-1].message_id)
    current = store.get('P13')
    assert current.deadline.at == math.ceil((current.order.at.at + 360) / 60) * 60

    explicit = run('send', 'worker', str(path), '--due', '23:59', '--clean',
                   '--replaces', current.messages[-1].message_id)
    current = store.get('P13')
    assert current.deadline.clock() == '23:59:00'


@pytest.mark.parametrize('rates', ((), (15.0,)))
def test_measured_deadline_rounds_up_and_keeps_two_minute_floor(tmp_path, rates):
    measured = MeasuredDeadline(rates)
    assert measured.deadline(order(tmp_path, 'new-order')).at == 240


def test_empty_measurement_does_not_create_a_store(tmp_path):
    store = TodoStore(tmp_path)
    measured = MeasuredDeadline.read(store)
    assert measured.rates == ()
    assert not store.path.exists()


@pytest.mark.parametrize('clock', ('', '24:00', '12:60', 'noon'))
def test_invalid_explicit_clock_is_refused_before_reading_the_order(commander_script, clock):
    context, repo = commander_script
    result = subprocess.run(
        [str(Path(sys.executable).with_name('agent-comms-commander')),
         '--root', str(context.comms.root), 'order', 'problem', str(repo / 'must-not-be-read'),
         '--to', 'worker', '--due', clock, '--where', f'{__file__}:1'],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
    assert result.returncode != 0
    assert 'FileNotFoundError' not in result.stderr
    assert context.comms.goals.todos.for_commander(context.incarnation) == ()
