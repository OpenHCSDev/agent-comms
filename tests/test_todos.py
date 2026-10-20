"""Todo creation and assignment retain exact owners, revisions and goal references."""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from dataclasses import replace
from multiprocessing import Manager
from pathlib import Path

import pytest

from agent_comms.change_report import FileLine
from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import OrderCheck
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.registration import Registration
from agent_comms.threads import Thread
from agent_comms.todos import (
    AssignTodo, Assignment, ChangeTodoState, DoneTodoState, GoalRef,
    MechanicalOrderRun, PendingProblemOrder,
    BlockedTodoState, DeliverTodo, OperationDelivery, ReleaseTodo, StrikeReason,
    Todo, TodoConflict, TodoError, TodoState, TodoStore, WithdrawTodo,
)

from cutover.carry_todo_open_indexes import TodoOpenIndexes


def thread(name: str, root: Path, created: float) -> Thread:
    thread_registration = Registration(Path(root) / "registry.json")
    return thread_registration.new_thread(name, frozenset(), str(root), created_at=created)


def order(root: Path):
    return PendingProblemOrder(id="checked-order", owner=FileLine(__file__, 1), run=MechanicalOrderRun(),
        check=OrderCheck(str(root), "true"), edits=("Fix the problem",),
        banned=("No extra store",), report="Report the delivered commit",
        at=RecordedEventTime(10.0), first_call_deadline=RecordedEventTime(130.0))


def _assign_process(root: str, name: str, created: float, gate) -> tuple[str, str]:
    store = TodoStore(Path(root))
    gate.wait(timeout=5)
    try:
        row = store.assign(
            "todo-1", expected_revision=1,
            owner=thread(name, Path(root), created), assigner=thread("lead", Path(root), 1.0).incarnation,
            generation=f"generation-{name}", goal=GoalRef("lead", 1.0, "goal-1"),
            deadline=RecordedEventTime(1000.0), order=order(Path(root)),
        )
        return "won", row.assignment.owner
    except TodoConflict as error:
        return "conflict", error.current.assignment.owner


def test_create_is_idempotent_and_goal_reference_is_historical(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    goal = GoalRef("lead", 1.0, "goal-1")
    original = store.create(
        "todo-1", RepositoryName.from_wire("OpenHCSDev/agent-comms"), "Fix the sidebar", creator=lead, goal=goal,
        owner=lead, generation="initial", deadline=RecordedEventTime(1000.0), order=order(tmp_path),
    )
    assert original.repo == RepositoryName.from_wire("OPENHCSDEV/agent-comms")
    assert original.revision == 1
    assert original.assignment == Assignment("lead", 1.0, "lead", 1.0, "initial")
    assert (
        store.create(
            "todo-1", RepositoryName.from_wire("openhcsdev/agent-comms"), "Fix the sidebar", creator=lead, goal=goal,
            owner=lead, generation="initial", deadline=RecordedEventTime(1000.0), order=order(tmp_path),
        )
        == original
    )
    with pytest.raises(TodoConflict):
        store.create(
            "todo-1", RepositoryName.from_wire("openhcsdev/agent-comms"), "A different task", creator=lead, goal=goal,
            owner=lead, generation="initial", deadline=RecordedEventTime(1000.0), order=order(tmp_path),
        )
    assert TodoStore(tmp_path).list(RepositoryName.from_wire("openhcsdev/agent-comms")) == (original,)
    newer = thread("lead", tmp_path, 2.0)
    assert store.get(original.id).goal == goal
    with pytest.raises(TodoConflict):
        store.apply("todo-1", ChangeTodoState(1, DoneTodoState, newer.incarnation, None))
    assert store.get("todo-1") == original


def test_withdraw_retires_a_released_problem(tmp_path):
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    worker = thread("worker", tmp_path, 2.0)
    todo = store.create("P1", RepositoryName.from_wire("org/repo"), "Obsolete problem",
        creator=lead, owner=worker, generation="first", goal=GoalRef("lead", 1.0, "request"),
        deadline=RecordedEventTime(1000), order=order(tmp_path))
    released = store.apply(todo.id, ReleaseTodo(todo.revision, lead.incarnation, todo.held(),
        StrikeReason("The previous worker stopped before completing this assigned repair on main.")))
    previous = released.holder_state.previous_assignment()
    assert previous == todo.held()
    why = "The requested repair already merged into main through another problem."
    with pytest.raises(TodoConflict):
        store.apply(todo.id, WithdrawTodo(released.revision, lead.incarnation,
            replace(previous, generation="wrong"), why))
    withdrawn = store.apply(todo.id, WithdrawTodo(released.revision, lead.incarnation,
        previous, why))
    assert store.for_commander(lead.incarnation) == ()
    assert withdrawn.state is DoneTodoState and not withdrawn.pending
    assert withdrawn.order.previous == todo.held()
    assert withdrawn.released == released.released and withdrawn.strikes == ()
    assert "withdrawn from worker, no strike" in withdrawn.order.withdrawals(withdrawn)[0]
    assert FieldCodec.decode(Todo, FieldCodec.encode(withdrawn)) == withdrawn


@pytest.mark.parametrize("released", (False, True))
def test_withdraw_refuses_a_delivered_problem(tmp_path, released):
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    todo = store.create("P1", RepositoryName.from_wire("org/repo"), "Delivered problem",
        creator=lead, owner=lead, generation="first", goal=GoalRef("lead", 1.0, "request"),
        deadline=RecordedEventTime(1000), order=order(tmp_path))
    delivered = store.apply(todo.id, DeliverTodo(todo.revision, lead.incarnation,
        OperationDelivery(at=RecordedEventTime(20)), todo.order.id))
    if released:
        delivered = store.apply(todo.id, ReleaseTodo(delivered.revision, lead.incarnation,
            todo.held(), StrikeReason("The worker finished its delivery and no longer needs this assignment.")))
    with pytest.raises(ValueError, match="Only an undelivered order can be withdrawn"):
        store.apply(todo.id, WithdrawTodo(delivered.revision, lead.incarnation,
            delivered.holder_state.previous_assignment(), "This delivery must remain on record."))
    assert store.get(todo.id) == delivered


def test_two_processes_assign_one_todo_before_any_worker_is_started(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    store.create(
        "todo-1", RepositoryName.from_wire("OpenHCSDev/agent-comms"), "One implementation", creator=lead, goal=GoalRef("lead", 1.0, "goal-1"),
        owner=lead, generation="initial", deadline=RecordedEventTime(1000.0), order=order(tmp_path),
    )
    with Manager() as manager, ProcessPoolExecutor(max_workers=2) as pool:
        gate = manager.Event()
        results = [
            pool.submit(_assign_process, str(tmp_path), "worker-a", 2.0, gate),
            pool.submit(_assign_process, str(tmp_path), "worker-b", 3.0, gate),
        ]
        gate.set()
        observed = [future.result(timeout=10) for future in results]
    assert sorted(result[0] for result in observed) == ["conflict", "won"]
    assert observed[0][1] == observed[1][1]
    persisted = TodoStore(tmp_path).get("todo-1")
    assert persisted.assignment.owner == observed[0][1]
    assert persisted.revision == 2
    assert len(store.list(RepositoryName.from_wire("OpenHCSDev/agent-comms"))) == 1


def test_validation_rejects_ambiguous_identity_and_bad_state(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    for index, repo in enumerate(("org/repo/branch", "../repo", "org/", "org/repo name")):
        with pytest.raises(ValueError):
            store.create(
                f"invalid-{index}", RepositoryName.from_wire(repo), "task", creator=lead, goal=GoalRef("lead", 1.0, "goal-1"),
                owner=lead, generation="initial", deadline=RecordedEventTime(1000.0), order=order(tmp_path),
            )
    with pytest.raises(TodoError):
        GoalRef("lead", float("nan"), "goal-1")
    store.create(
        "todo-1", RepositoryName.from_wire("org/repo"), "task", creator=lead, goal=GoalRef("lead", 1.0, "goal-1"),
        owner=lead, generation="initial", deadline=RecordedEventTime(1000.0), order=order(tmp_path),
    )
    with pytest.raises(ValueError):
        FieldCodec.decode(type[TodoState], "unverified")


def test_merged_order_keeps_its_checked_fields(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    todo = store.create(
        "todo-1", RepositoryName.from_wire("OpenHCSDev/agent-comms"), "Fix the sidebar", creator=lead, goal=GoalRef("lead", 1.0, "goal-1"),
        owner=lead, generation="initial", deadline=RecordedEventTime(1000.0), order=order(tmp_path))
    merged = replace(todo, order=todo.order.checked().merged("2b9c1f1ab"))
    cells = merged.order.board_cells(merged)
    assert cells.startswith(f"{__file__}:1 | lead | checked-order | {RecordedEventTime(1000.0).clock()} | ")
    assert json.loads(cells.split(" | ", 4)[-1]) == FieldCodec.encode(merged.order.checked().check)
    assert merged.order.checked().copy_as(PendingProblemOrder) == todo.order
    assert merged.order.line == "Merged and installed"
    assert merged.order.waiting_on_install(merged, {}) == ()
