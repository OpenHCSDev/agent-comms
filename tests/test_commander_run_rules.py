"""The declared run and assignment history stay with their original records."""
from dataclasses import replace

import pytest

from agent_comms.change_report import FileLine
from agent_comms.errors import ToolRefusedError
from agent_comms.field_codec import FieldCodec
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import GoalRef, Todo, TodoStore
from agent_comms.order_runs import ApplicationOrderRun
from test_todos import order, thread


def test_declared_run_round_trips_and_short_runs_are_refused(tmp_path):
    planned = 'The real application shows every thread after reopening the saved session'
    checked = replace(order(tmp_path), run=ApplicationOrderRun(planned))
    store = TodoStore(tmp_path)
    lead = thread('lead', tmp_path, 1)
    todo = store.create('P1', RepositoryName.from_wire('OpenHCSDev/agent-comms'),
        'Repair the thread list',
        creator=lead, owner=lead, generation='one', goal=GoalRef('lead', 1, 'one'), review_at=RecordedEventTime(1000),
        order=checked)
    assert FieldCodec.decode(Todo, FieldCodec.encode(todo)) == todo
    assert planned in todo.order.instructions(todo)
    with pytest.raises(ToolRefusedError):
        ApplicationOrderRun('a grep passed')
    with pytest.raises(ToolRefusedError):
        checked.run.require_artifact(str(tmp_path / 'missing.png'))
    with pytest.raises(ToolRefusedError):
        checked.run.require_closing('The unrelated result does not describe the requested application run.')
    checked.run.require_closing(planned)


def test_clean_transfers_keep_history_and_third_owner_requires_a_trace(tmp_path):
    store = TodoStore(tmp_path)
    lead = thread('lead', tmp_path, 1)
    first = store.create('P1', RepositoryName.from_wire('OpenHCSDev/agent-comms'),
        'Repair the thread list',
        creator=lead, owner=lead, generation='one', goal=GoalRef('lead', 1, 'one'), review_at=RecordedEventTime(1000),
        order=order(tmp_path))
    second = store.assign('P1', expected_revision=first.revision,
        owner=thread('second', tmp_path, 2), assigner=lead.incarnation, generation='two', goal=GoalRef('second', 2, 'two'),
        review_at=RecordedEventTime(1000), order=order(tmp_path))
    assert not second.strikes
    kwargs = dict(expected_revision=second.revision, owner=thread('third', tmp_path, 3),
        assigner=lead.incarnation, generation='three', goal=GoalRef('third', 3, 'three'), review_at=RecordedEventTime(1000), order=order(tmp_path))
    with pytest.raises(ValueError):
        store.assign('P1', **kwargs)
    # Source existence belongs to the commander boundary, not a Core record.
    traced = FileLine('source-not-local-to-core.py', 999999)
    third = store.assign('P1', traced=traced, **kwargs)
    assert [old.name for old in third.assignment.previous_owners] == ['lead', 'second']
    assert store.assign('P1', traced=traced, **kwargs) == third


