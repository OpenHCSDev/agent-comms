"""The declared run and assignment history stay with their original records."""
from dataclasses import replace

import pytest

from agent_comms.change_report import FileLine
from agent_comms.field_codec import FieldCodec
from agent_comms.plan_phases import RecordPlanePhase
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


def test_clean_transfers_keep_history_and_third_owner_requires_a_trace(tmp_path):
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
    # Source existence belongs to the commander boundary, not a Core record.
    traced = FileLine('source-not-local-to-core.py', 999999)
    third = store.assign('P1', traced=traced, **kwargs)
    assert [old.name for old in third.assignment.previous_owners] == ['lead', 'second']
    assert store.assign('P1', traced=traced, **kwargs) == third


