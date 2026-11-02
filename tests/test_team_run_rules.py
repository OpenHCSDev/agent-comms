"""The declared run and assignment history stay with their original records."""
from dataclasses import replace

import pytest

from agent_comms.change_report import FileLine
from agent_comms.nominal.field_codec import FieldCodec, GoalId
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import ExpectedProblem, Todo, TodoStore
from agent_comms.order_runs import ApplicationOrderRun
from agent_comms_hooks import ToolRefusedError
from test_todos import order, thread


def test_declared_run_round_trips_and_short_runs_are_refused(tmp_path, source_repository):
    one_goal_id = GoalId.mint()
    planned = 'The real application shows every thread after reopening the saved session'
    checked = replace(order(source_repository), run=ApplicationOrderRun(planned))
    store = TodoStore(tmp_path)
    lead = thread('lead', tmp_path, 1)
    request = store.add_request(lead.incarnation, 'Repair the thread list').id
    todo = store.create('P1', RepositoryName.from_wire('OpenHCSDev/agent-comms'),
        'Repair the thread list',
        creator=lead, assigner=lead.incarnation, owner=lead, generation=one_goal_id,
        request=request, review_at=RecordedEventTime(1000), order=checked)
    assert FieldCodec.decode(Todo, FieldCodec.encode(todo)) == todo
    assert planned in todo.order.instructions(todo)
    with pytest.raises(ToolRefusedError):
        ApplicationOrderRun('a grep passed')
    with pytest.raises(ToolRefusedError):
        checked.run.require_artifact(str(tmp_path / 'missing.png'))
    with pytest.raises(ToolRefusedError):
        checked.run.require_closing('The unrelated result does not describe the requested application run.')
    checked.run.require_closing(planned)


def test_clean_transfers_keep_history_and_third_owner_requires_a_trace(tmp_path, source_repository):
    one_goal_id = GoalId.mint()
    three_goal_id = GoalId.mint()
    two_goal_id = GoalId.mint()
    store = TodoStore(tmp_path)
    lead = thread('lead', tmp_path, 1)
    request = store.add_request(lead.incarnation, 'Repair the thread list').id
    first = store.create('P1', RepositoryName.from_wire('OpenHCSDev/agent-comms'),
        'Repair the thread list',
        creator=lead, assigner=lead.incarnation, owner=lead, generation=one_goal_id,
        request=request, review_at=RecordedEventTime(1000), order=order(source_repository))
    second, = store.assign((ExpectedProblem('P1', first.revision),),
        owner=thread('second', tmp_path, 2), assigner=lead.incarnation, generation=two_goal_id,
            request=request,
        review_at=RecordedEventTime(1000), order=order(source_repository))
    assert second.strike_count == 0
    kwargs = dict(owner=thread('third', tmp_path, 3),
        assigner=lead.incarnation, generation=three_goal_id, request=request,
            review_at=RecordedEventTime(1000), order=order(source_repository))
    problems = (ExpectedProblem('P1', second.revision),)
    with pytest.raises(ValueError):
        store.assign(problems, **kwargs)
    # Source existence belongs to the commander boundary, not a Core record.
    traced = FileLine('source-not-local-to-core.py', 999999)
    third, = store.assign(problems, traced=traced, **kwargs)
    assert [old.name for old in third.assignment.previous_owners] == ['lead', 'second']
    assert store.assign(problems, traced=traced, **kwargs) == (third,)


