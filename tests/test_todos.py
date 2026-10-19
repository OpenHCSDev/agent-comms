"""Todo creation and assignment retain exact owners, revisions and goal references."""

from __future__ import annotations

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
from agent_comms.registration import Registration
from agent_comms.threads import Thread
from agent_comms.todos import (
    Assignment, ChangeTodoState, DoneTodoState, GoalRef, MechanicalOrderRun, PendingProblemOrder,
    BlockedTodoState, OperationDelivery, Todo, TodoConflict, TodoError, TodoState, TodoStore,
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
            owner=thread(name, Path(root), created), parent=thread("lead", Path(root), 1.0),
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
        "todo-1", "OpenHCSDev/agent-comms", "Fix the sidebar", creator=lead, goal=goal,
        owner=lead, generation="initial", deadline=RecordedEventTime(1000.0), order=order(tmp_path),
    )
    assert original.repo == "openhcsdev/agent-comms"
    assert original.revision == 1
    assert original.assignment == Assignment("lead", 1.0, "lead", 1.0, "initial")
    assert (
        store.create(
            "todo-1", "openhcsdev/agent-comms", "Fix the sidebar", creator=lead, goal=goal,
            owner=lead, generation="initial", deadline=RecordedEventTime(1000.0), order=order(tmp_path),
        )
        == original
    )
    with pytest.raises(TodoConflict):
        store.create(
            "todo-1", "openhcsdev/agent-comms", "A different task", creator=lead, goal=goal,
            owner=lead, generation="initial", deadline=RecordedEventTime(1000.0), order=order(tmp_path),
        )
    assert TodoStore(tmp_path).list("openhcsdev/agent-comms") == (original,)
    newer = thread("lead", tmp_path, 2.0)
    assert store.get(original.id).goal == goal
    with pytest.raises(TodoConflict):
        store.apply("todo-1", ChangeTodoState(1, DoneTodoState, newer.incarnation, None))
    assert store.get("todo-1") == original


def test_commander_reads_decode_only_open_todos(tmp_path, monkeypatch):
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    first = store.create("P1", "org/repo", "Open order", creator=lead, owner=lead,
        generation="one", goal=GoalRef("lead", 1.0, "request"),
        deadline=RecordedEventTime(1000), order=order(tmp_path))
    second = replace(first, id="P2", state=BlockedTodoState)
    delivered = first.order.delivered(OperationDelivery(at=RecordedEventTime(20))).installed(
        "The installed application now shows every expected saved message correctly.", "evidence")
    with store.writing() as db:
        second.insert(db)
        for number in range(3, 303):
            replace(first, id=f"P{number}", state=DoneTodoState,
                assignment=None, order=delivered).insert(db)
    with closing(CoordinationStore.writer(store.path)) as db:
        indexes = tuple(name for name, in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
        for name in indexes:
            db.execute(f'DROP INDEX "{name}"')
        db.commit()
    assert TodoOpenIndexes.carry(tmp_path, apply=False).changed == 2
    assert TodoOpenIndexes.carry(tmp_path, apply=True).changed == 2
    assert TodoOpenIndexes.carry(tmp_path, apply=True).changed == 0

    decoded = []
    construct = FieldCodec.construct

    def count(target, values):
        if target is Todo:
            decoded.append(values["id"])
        return construct(target, values)

    monkeypatch.setattr(FieldCodec, "construct", staticmethod(count))
    assert store.for_commander(lead.incarnation) == (first, second)
    assert decoded == ["P1", "P2"]
    decoded.clear()
    assert store.holds_pending(lead.incarnation)
    assert decoded == ["P1", "P2"]
    decoded.clear()
    assert store.held_for_commander(lead.incarnation) == (first, second)
    assert decoded == ["P1", "P2"]
    decoded.clear()
    assert store.held_by(lead.incarnation, "lead") == (first, second)
    assert decoded == ["P1", "P2"]
    decoded.clear()
    assert store.held_by(lead.incarnation, "another-unit") == ()
    assert decoded == []
    with store.snapshot() as db:
        states = TodoState.open_names()
        placeholders = ','.join('?' for state in states)
        plans = tuple(db.execute(f"EXPLAIN QUERY PLAN SELECT * FROM todo "
            f"WHERE creator=? AND creator_created=? AND state IN ({placeholders})",
            (lead.name, lead.created_at, *states)))
        assert any("USING INDEX" in row[-1] for row in plans)


def test_two_processes_assign_one_todo_before_any_worker_is_started(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    store.create(
        "todo-1", "OpenHCSDev/agent-comms", "One implementation", creator=lead, goal=GoalRef("lead", 1.0, "goal-1"),
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
    assert len(store.list("OpenHCSDev/agent-comms")) == 1


def test_validation_rejects_ambiguous_identity_and_bad_state(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    for index, repo in enumerate(("org/repo/branch", "../repo", "org/", "org/repo name")):
        with pytest.raises(TodoError):
            store.create(
                f"invalid-{index}", repo, "task", creator=lead, goal=GoalRef("lead", 1.0, "goal-1"),
                owner=lead, generation="initial", deadline=RecordedEventTime(1000.0), order=order(tmp_path),
            )
    with pytest.raises(TodoError):
        GoalRef("lead", float("nan"), "goal-1")
    store.create(
        "todo-1", "org/repo", "task", creator=lead, goal=GoalRef("lead", 1.0, "goal-1"),
        owner=lead, generation="initial", deadline=RecordedEventTime(1000.0), order=order(tmp_path),
    )
    with pytest.raises(ValueError):
        FieldCodec.decode(type[TodoState], "unverified")
