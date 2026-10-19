"""State-level sanity checks, not the required native startup/first-reply proof."""

from dataclasses import replace

import pytest

from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.plan_phases import RecordPlanePhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import DoneTodoState, OperationDelivery, StrikeReason
from agent_comms.work_dispatch import CheckedPhaseTask, UnspecifiedPhaseTask, WorkDispatch

from test_commander_problem_orders import create, order


def test_phase_task_accepts_only_its_full_problem_with_installed_evidence(commander):
    comms, _ = commander
    full, subtask = (create(commander, name) for name in ("P1", "P2"))
    task = CheckedPhaseTask(full.id, full.repo, full.text, full.order, 1800)
    accepted = full.order.delivered(
        OperationDelivery(at=RecordedEventTime(200)),
    ).installed("The installed application now completes the declared full phase workflow.", "run.txt")
    assert not task.done((full, subtask))
    assert not task.done((replace(subtask, state=DoneTodoState, order=accepted),))
    assert not task.done((replace(full, state=DoneTodoState),))
    assert not task.done((replace(full, order=accepted),))
    assert task.done((replace(full, state=DoneTodoState, order=accepted),))
    assert not UnspecifiedPhaseTask().done((replace(full, state=DoneTodoState, order=accepted),))


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_checked_task_requires_a_finite_positive_deadline(commander, interval):
    comms, _ = commander
    with pytest.raises(ValueError, match="finite positive"):
        CheckedPhaseTask("P1", "org/repo", "Full phase", order(comms.root, "full"), interval)


def test_unspecified_phase_cannot_allocate_or_fabricate_an_order(commander):
    comms, _ = commander
    lead = comms.registry.require("lead")
    before = comms.registry.all_threads()
    task = UnspecifiedPhaseTask()
    task.start(comms, lead, RecordPlanePhase)
    assert comms.registry.all_threads() == before
    assert comms.goals.todos.history_for_commander(lead.incarnation) == ()


def test_invalid_request_refuses_dispatch_before_allocating_anything(commander):
    comms, _ = commander
    checked = order(comms.root, "full-phase-order")
    action = CreateProblemGoalAction(
        problem="P1", repo="OpenHCSDev/agent-comms", text="Repair the full phase",
        order=checked, request="not-the-active-request", commander="lead",
        deadline=RecordedEventTime(1000),
    )
    name = "unit-p1-r0"
    path = comms.registry.storage_rule.order_checkout(name, "P1")
    assert not path.exists()
    before = comms.registry.all_threads()
    with pytest.raises(ValueError):
        WorkDispatch(action, revision=0).start(comms)
    assert not path.exists()
    assert comms.registry.all_threads() == before


def test_released_order_preserves_the_two_owner_trace_requirement(commander):
    comms, _ = commander
    lead, worker = (comms.registry.require(name) for name in ("lead", "worker"))
    second, third = (
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(comms.root)))
        for name in ("second", "third")
    )
    todo = create(commander, "P1")
    reason = StrikeReason("The previous owner stopped work and left the checked order unfinished.")
    comms.goals.todos.fire(worker.name, lead.incarnation, reason)
    released = comms.goals.todos.get(todo.id)
    assigned = comms.goals.todos.assign(
        todo.id, expected_revision=released.revision, owner=second, parent=lead,
        generation="second-generation", goal=todo.goal,
        deadline=todo.deadline, order=todo.order.reissued(200),
    )
    assert assigned.holder == second.name
    comms.goals.todos.fire(second.name, lead.incarnation, reason)
    released = comms.goals.todos.get(todo.id)
    with pytest.raises(ValueError, match="pass --traced"):
        comms.goals.todos.assign(
            todo.id, expected_revision=released.revision, owner=third, parent=lead,
            generation="third-generation", goal=todo.goal,
            deadline=todo.deadline, order=todo.order.reissued(300),
        )
    assert comms.goals.todos.get(todo.id) == released
