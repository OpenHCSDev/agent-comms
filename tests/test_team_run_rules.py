"""The declared run and assignment history stay with their original records."""
import time
from dataclasses import replace

import pytest

from agent_comms.nominal.field_codec import FieldCodec, GoalId, OrderId
from agent_comms.planning.goal_actions import OrderGoalAction
from agent_comms.planning.order_runs import ApplicationOrderRun
from agent_comms.planning.orders import (
    Todo,
    TodoStore,
)
from agent_comms.projects.change_report import FileLine
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms_hooks import ToolRefusedError
from test_forks import fork_bus  # noqa: F401
from test_todos import order, thread


def test_declared_run_round_trips_and_short_runs_are_refused(tmp_path, source_repository):
    one_goal_id = GoalId.mint()
    planned = 'The real application shows every thread after reopening the saved session'
    checked = replace(order(source_repository), run=ApplicationOrderRun(planned))
    store = TodoStore(tmp_path)
    colleague = thread('colleague', tmp_path, 1)
    request = store.add_request(colleague.incarnation, 'Repair the thread list').id
    todo = store.create('P1', RepositoryName.from_wire('OpenHCSDev/agent-comms'),
        'Repair the thread list',
        creator=colleague, assigner=colleague.incarnation, owner=colleague, generation=one_goal_id,
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


def test_clean_transfers_keep_history_and_third_owner_requires_a_trace(fork_bus):
    """Each transfer is its holder handing the work on (holder consent, thread_status.py:91);
    the third distinct owner must name the failing path it read."""
    comms, _, _ = fork_bus

    def hand_on(receiver: str, holder: str, **traced):
        current = comms.goals.todos.get("P40")
        comms.goals.update_goal(receiver, OrderGoalAction(
            problem=current.id, expected_revision=current.revision,
            assigner=comms.registry.require(holder).acting_identity,
            request=current.request.require(), review_at=RecordedEventTime(time.time() + 600),
            order=replace(current.order, id=OrderId.mint()), **traced))
        return comms.goals.todos.get("P40")

    second = hand_on("worker", "retained")
    assert [old.name for old in second.held().previous_owners] == ["retained"]
    with pytest.raises(ValueError):
        hand_on("other", "worker")
    assert comms.goals.todos.get("P40") == second
    # Source existence belongs to the order author's boundary, not a Core record.
    third = hand_on("other", "worker", traced=FileLine("source-not-local-to-core.py", 999999))
    assert third.held().owner_identity == comms.registry.require("other").incarnation
    assert [old.name for old in third.held().previous_owners] == ["retained", "worker"]
