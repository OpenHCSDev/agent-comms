"""The declared run and assignment history stay with their original records."""
from dataclasses import replace

import pytest

from agent_comms.change_report import FileLine
from agent_comms.field_codec import FieldCodec
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import ApplicationOrderRun, AssignTodo, GoalRef, MechanicalOrderRun, Todo, TodoStore
from test_todos import order, thread


def test_declared_run_round_trips_and_short_runs_are_refused(tmp_path):
    planned = 'The real application shows every thread after reopening the saved session'
    checked = replace(order(tmp_path), run=ApplicationOrderRun(planned))
    store = TodoStore(tmp_path)
    lead = thread('lead', tmp_path, 1)
    todo = store.create('P1', RepositoryName.from_wire('OpenHCSDev/agent-comms'),
        'Repair the thread list',
        creator=lead, owner=lead, generation='one', goal=GoalRef('lead', 1, 'one'), deadline=RecordedEventTime(1000),
        order=checked)
    assert FieldCodec.decode(Todo, FieldCodec.encode(todo)) == todo
    assert planned in todo.order.instructions(todo)
    with pytest.raises(ValueError, match='8\\+ words'):
        ApplicationOrderRun('a grep passed')


def test_clean_transfers_keep_history_and_third_owner_needs_a_real_line(tmp_path):
    store = TodoStore(tmp_path)
    lead = thread('lead', tmp_path, 1)
    first = store.create('P1', RepositoryName.from_wire('OpenHCSDev/agent-comms'),
        'Repair the thread list',
        creator=lead, owner=lead, generation='one', goal=GoalRef('lead', 1, 'one'), deadline=RecordedEventTime(1000),
        order=order(tmp_path))
    second = store.assign('P1', expected_revision=first.revision,
        owner=thread('second', tmp_path, 2), assigner=lead.incarnation, generation='two', goal=GoalRef('second', 2, 'two'),
        deadline=RecordedEventTime(1000), order=order(tmp_path))
    assert not second.strikes
    kwargs = dict(expected_revision=second.revision, owner=thread('third', tmp_path, 3),
        assigner=lead.incarnation, generation='three', goal=GoalRef('third', 3, 'three'), deadline=RecordedEventTime(1000), order=order(tmp_path))
    with pytest.raises(ValueError, match='--traced PATH:LINE'):
        store.assign('P1', **kwargs)
    with pytest.raises(ValueError, match='does not exist'):
        store.assign('P1', traced=FileLine(__file__, 999999), **kwargs)
    third = store.assign('P1', traced=FileLine(__file__, 1), **kwargs)
    assert [old.name for old in third.assignment.previous_owners] == ['lead', 'second']
    assert store.assign('P1', traced=FileLine(__file__, 1), **kwargs) == third


def test_send_requires_a_declared_run_including_mechanical_orders(commander_script):
    import json
    from agent_comms.commander.order import OrderFile
    script, repo = commander_script
    data = {'defect': 'The real thread list disappears after opening saved history',
            'why_stupid': 'The repeated parser is wrong because Core declares these fields.',
            'order': {'kind': 'pending', 'owner': f'{__file__}:1',
                      'check': {'directory': str(repo), 'command': 'printf 0',
                                'result': {'kind': 'count', 'expected': 1}},
                      'edits': ['Repair the counter value.'],
                      'banned': ['no second store'], 'report': 'the count'}}
    with pytest.raises(SystemExit, match='run: Missing required fields'):
        OrderFile.read(json.dumps(data))
    assert script.todos() == ()
    data['order']['run'] = {'kind': 'mechanical'}
    script.send('worker', '23:59', OrderFile.read(json.dumps(data)), clean=True,
                new=('P1', data['defect']))
    assert isinstance(script.comms.goals.todos.get('P1').order.run, MechanicalOrderRun)
