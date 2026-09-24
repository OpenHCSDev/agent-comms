"""Post-PR1 todo assignment is durable, one-owner and independent of goals."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from multiprocessing import Manager
from pathlib import Path

import pytest

from agent_comms import Thread
from agent_comms.todos import Assignment, GoalRef, TodoConflict, TodoError, TodoStore


def thread(name: str, root: Path, created: float) -> Thread:
    return Thread(name, frozenset(), str(root), created_at=created)


def _assign_process(root: str, name: str, created: float, gate) -> tuple[str, str]:
    store = TodoStore(Path(root))
    gate.wait(timeout=5)
    try:
        row = store.assign(
            "todo-1",
            expected_revision=1,
            owner=thread(name, Path(root), created),
            parent=thread("lead", Path(root), 1.0),
            generation=f"generation-{name}",
        )
        return "won", row.assignment.owner
    except TodoConflict as error:
        return "conflict", error.current.assignment.owner


def test_create_is_idempotent_and_goal_reference_is_historical(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    goal = GoalRef("lead", 1.0, "goal-1")
    original = store.create(
        "todo-1", "OpenHCSDev/agent-comms", "Fix the sidebar", creator=lead, goal=goal
    )
    assert original.repo == "openhcsdev/agent-comms"
    assert original.revision == 1 and original.assignment is None
    assert (
        store.create("todo-1", "openhcsdev/agent-comms", "Fix the sidebar", creator=lead, goal=goal)
        == original
    )
    with pytest.raises(TodoConflict):
        store.create("todo-1", "openhcsdev/agent-comms", "A different task", creator=lead)
    assert TodoStore(tmp_path).list("openhcsdev/agent-comms") == (original,)
    # A new same-name goal owner does not rebind this todo's saved old goal.
    newer = thread("lead", tmp_path, 2.0)
    assert store.get(original.id).goal == goal
    with pytest.raises(TodoConflict):
        store.set_state("todo-1", expected_revision=1, state="done", actor=newer)
    assert store.get("todo-1") == original


def test_two_processes_assign_one_todo_before_any_worker_is_started(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    store.create(
        "todo-1",
        "OpenHCSDev/agent-comms",
        "One implementation",
        creator=thread("lead", tmp_path, 1.0),
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


def test_idempotent_retry_transfer_and_old_generation_cannot_clear_owner(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    first = thread("worker-a", tmp_path, 2.0)
    second = thread("worker-b", tmp_path, 3.0)
    store.create("todo-1", "org/repo", "One task", creator=lead)
    reserved = store.assign(
        "todo-1", expected_revision=1, owner=first, parent=lead, generation="gen-a"
    )
    assert (
        store.assign("todo-1", expected_revision=1, owner=first, parent=lead, generation="gen-a")
        == reserved
    )
    with pytest.raises(TodoConflict) as conflict:
        store.assign("todo-1", expected_revision=1, owner=second, parent=lead, generation="gen-b")
    assert conflict.value.current.assignment == reserved.assignment
    moved = store.transfer(
        "todo-1",
        expected_revision=2,
        previous=reserved.assignment,
        owner=second,
        parent=lead,
        generation="gen-b",
    )
    assert moved.revision == 3 and moved.assignment.owner == "worker-b"
    store = TodoStore(tmp_path)  # Retry after the writer process has exited.
    assert (
        store.transfer(
            "todo-1",
            expected_revision=2,
            previous=reserved.assignment,
            owner=second,
            parent=lead,
            generation="gen-b",
        )
        == moved
    )
    with pytest.raises(TodoConflict):
        store.transfer(
            "todo-1",
            expected_revision=2,
            previous=Assignment("other", 4.0, "lead", 1.0, "gen-other"),
            owner=second,
            parent=lead,
            generation="gen-b",
        )
    with pytest.raises(TodoConflict):
        store.release("todo-1", expected_revision=3, previous=reserved.assignment)
    with pytest.raises(TodoConflict):
        store.transfer(
            "todo-1",
            expected_revision=3,
            previous=reserved.assignment,
            owner=first,
            parent=lead,
            generation="gen-c",
        )
    assert TodoStore(tmp_path).get("todo-1") == moved


def test_release_exact_retry_after_uncertain_reply(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    worker = thread("worker", tmp_path, 2.0)
    store.create("todo-1", "org/repo", "One task", creator=lead)
    reserved = store.assign(
        "todo-1", expected_revision=1, owner=worker, parent=lead, generation="gen-a"
    )
    released = store.release("todo-1", expected_revision=2, previous=reserved.assignment)
    assert released.revision == 3 and released.assignment is None
    store = TodoStore(tmp_path)
    assert store.release("todo-1", expected_revision=2, previous=reserved.assignment) == released
    with pytest.raises(TodoConflict):
        store.release(
            "todo-1",
            expected_revision=2,
            previous=Assignment("other", 4.0, "lead", 1.0, "gen-other"),
        )
    with pytest.raises(TodoConflict):
        store.release("todo-1", expected_revision=1, previous=reserved.assignment)


def test_blocked_todo_retains_assignment_goal_does_not_follow_status(
    tmp_path: Path,
) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    worker = thread("worker", tmp_path, 2.0)
    goal = GoalRef("lead", 1.0, "goal-1")
    store.create("todo-1", "org/repo", "Work A", creator=lead, goal=goal)
    store.create("todo-2", "org/repo", "Work B", creator=lead, goal=goal)
    assigned = store.assign(
        "todo-1", expected_revision=1, owner=worker, parent=lead, generation="gen-a"
    )
    blocked = store.set_state("todo-1", expected_revision=2, state="blocked", actor=lead)
    assert blocked.assignment == assigned.assignment
    with pytest.raises(TodoConflict):
        store.transfer(
            "todo-1",
            expected_revision=3,
            previous=assigned.assignment,
            owner=lead,
            parent=lead,
            generation="gen-b",
        )
    opened = store.set_state("todo-1", expected_revision=3, state="open", actor=lead)
    with pytest.raises(TodoConflict):
        store.set_state("todo-1", expected_revision=4, state="done", actor=lead)
    with pytest.raises(TodoConflict):
        store.set_state(
            "todo-1",
            expected_revision=4,
            state="done",
            actor=worker,
            generation="wrong-generation",
        )
    done = store.set_state(
        "todo-1", expected_revision=4, state="done", actor=worker, generation="gen-a"
    )
    assert opened.assignment == assigned.assignment
    assert done.assignment is None and done.revision == 5
    assert store.get("todo-2").state == "open" and store.get("todo-2").goal == goal
    with pytest.raises(TodoConflict):
        store.assign("todo-1", expected_revision=5, owner=worker, parent=lead, generation="gen-c")


def test_validation_rejects_ambiguous_identity_and_bad_state(tmp_path: Path) -> None:
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    for index, repo in enumerate(("org/repo/branch", "../repo", "org/", "org/repo name")):
        with pytest.raises(TodoError):
            store.create(f"invalid-{index}", repo, "task", creator=lead)
    with pytest.raises(TodoError):
        GoalRef("lead", float("nan"), "goal-1")
    store.create("todo-1", "org/repo", "task", creator=lead)
    with pytest.raises(TodoError):
        store.set_state("todo-1", expected_revision=1, state="unverified", actor=lead)
