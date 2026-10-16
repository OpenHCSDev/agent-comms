"""Todo problems own exact assignments; Goals own checked order delivery."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from multiprocessing import Manager
from pathlib import Path
import subprocess
import time

import pytest

from agent_comms.activity import ActivityState
from agent_comms.cli_commands import FleetCliCommand
from agent_comms.comms import Comms
from agent_comms.goal_actions import ClearGoalAction, CompletedGoalAction, GoalPrecondition, OwnerInvocable, SetGoalAction
from agent_comms.goal_progress import GoalReport, ReportedGoalProgress, WorksInApp
from agent_comms.goals import Deadline, GoalRef
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.todos import (
    AcceptanceCheck, AssignedFleetEntry, Assignment, BlockedTodoState, DoneTodoState,
    FormerOrderFleetEntry, OpenTodoState, SourceTodoWhere, TodoConflict, TodoError,
    TodoRef, TodoStore, UnassignedFleetEntry, UnassignedTodoAssignment,
)


@pytest.fixture
def todo_comms(comms: Comms, tmp_path: Path) -> Comms:
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "problem.txt").write_text("P51_PENDING\nA real count is required.\n")
    for created, name in enumerate(("lead", "worker-a", "worker-b"), start=1):
        comms.registry.declare(Thread(name, frozenset(), str(repo), created_at=float(created)))
    return comms


def create(comms: Comms, todo_id: str = "todo-1"):
    lead = comms.registry.require("lead")
    repo = Path(lead.worktree)
    return TodoStore(comms.root).create(
        todo_id, "Org/Repo", "Remove the pending marker", creator=lead,
        where=SourceTodoWhere(str(repo / "problem.txt"), 1),
        acceptance=AcceptanceCheck(str(repo),
            'python3 -c \'from pathlib import Path; print(Path("problem.txt").read_text().count("P51_PENDING"))\'', 0),
        bus=comms.bus,
    )


def assign(comms: Comms, todo_id="todo-1", owner="worker-a", generation="order-a"):
    return TodoStore(comms.root).assign(
        todo_id, expected_revision=1, owner=comms.registry.require(owner),
        parent=comms.registry.require("lead"), generation=generation,
        deadline=Deadline(time.time() - 60), goals=comms.goals,
    )


def report(sha: str):
    return ReportedGoalProgress(GoalReport(
        "Unverified reports drift from the actual source.", "The pending marker.",
        WorksInApp("The declared count passed on the delivered clean commit.", sha),
        "Inspect the persisted problem and order.",
    ))


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), "-c", "user.name=Verification",
         "-c", "user.email=verify@example.invalid", *args),
        capture_output=True, text=True, timeout=15, check=True,
    )
    return result.stdout.strip()


def _assign_process(root: str, name: str, gate) -> tuple[str, str]:
    comms = Comms(Path(root))
    gate.wait(timeout=10)
    try:
        row = assign(comms, owner=name, generation=f"order-{name}")
        return "won", row.assignment.require_assigned(row).owner.name
    except TodoConflict as error:
        return "conflict", error.current.assignment.require_assigned(error.current).owner.name


def test_create_is_idempotent_and_has_no_order_until_assigned(todo_comms: Comms):
    original = create(todo_comms)
    store = TodoStore(todo_comms.root)
    assert original.repo == "org/repo" and original.revision == 1
    assert original.assignment == UnassignedTodoAssignment()
    assert create(todo_comms) == original
    assert todo_comms.registry.require("worker-a").goal is None
    assert store.list("Org/Repo") == (original,)
    with pytest.raises(TodoConflict):
        store.create(
            original.id, original.repo, "Different work", creator=todo_comms.registry.require("lead"),
            where=original.where, acceptance=original.acceptance, bus=todo_comms.bus,
        )


def test_two_processes_assign_one_problem_without_starting_native_workers(todo_comms: Comms):
    create(todo_comms)
    with Manager() as manager, ProcessPoolExecutor(max_workers=2) as pool:
        gate = manager.Event()
        futures = [pool.submit(_assign_process, str(todo_comms.root), name, gate)
                   for name in ("worker-a", "worker-b")]
        gate.set()
        observed = [future.result(timeout=30) for future in futures]
    assert sorted(result[0] for result in observed) == ["conflict", "won"]
    assert observed[0][1] == observed[1][1]
    persisted = TodoStore(todo_comms.root).get("todo-1")
    assert persisted.revision == 2
    assert persisted.assignment.require_assigned(persisted).owner.name == observed[0][1]


def test_transfer_frees_old_slot_preserves_history_and_is_exactly_repeatable(todo_comms: Comms):
    create(todo_comms)
    reserved = assign(todo_comms)
    previous = reserved.assignment.require_assigned(reserved)
    store = TodoStore(todo_comms.root)
    kwargs = dict(expected_revision=2, previous=previous,
                  owner=todo_comms.registry.require("worker-b"),
                  parent=todo_comms.registry.require("lead"), generation="order-b",
                  deadline=Deadline(time.time() + 3600), goals=todo_comms.goals)
    moved = store.transfer("todo-1", **kwargs)
    assert moved.revision == 3
    assert todo_comms.registry.require("worker-a").goal is None
    history = todo_comms.goals.goal_history("worker-a", goal_id="order-a")
    assert history[-1].before.id == "order-a" and history[-1].after is None
    create(todo_comms, "todo-2")
    reused = assign(todo_comms, "todo-2", generation="next-order-a")
    assert store.transfer("todo-1", **kwargs) == moved
    with todo_comms.goals.writing() as completed:
        ClearGoalAction.end_current(previous.goal, goals=todo_comms.goals, completed=completed)
    assert reused.assignment.require_assigned(reused).goal.require_current(todo_comms.registry).id == "next-order-a"
    with pytest.raises(TodoConflict):
        store.release("todo-1", expected_revision=3, previous=previous, goals=todo_comms.goals)
    with pytest.raises(TodoConflict):
        store.complete("todo-1", expected_revision=3, actor=todo_comms.registry.require("worker-a"),
                       progress=report("abcdef123456"), goals=todo_comms.goals, actor_kind=OwnerInvocable)


def test_release_clears_exact_order_and_repeat_does_not_clear_a_new_one(todo_comms: Comms):
    create(todo_comms)
    reserved = assign(todo_comms)
    previous = reserved.assignment.require_assigned(reserved)
    store = TodoStore(todo_comms.root)
    released = store.release("todo-1", expected_revision=2, previous=previous, goals=todo_comms.goals)
    assert released.revision == 3 and released.assignment == UnassignedTodoAssignment()
    assert todo_comms.registry.require("worker-a").goal is None
    create(todo_comms, "todo-2")
    assign(todo_comms, "todo-2", generation="another-order")
    assert store.release("todo-1", expected_revision=2, previous=previous, goals=todo_comms.goals) == released
    assert todo_comms.registry.require("worker-a").require_goal().id == "another-order"


def test_busy_target_refuses_before_changing_problem(todo_comms: Comms):
    first = create(todo_comms)
    assign(todo_comms)
    second = create(todo_comms, "todo-2")
    with pytest.raises(TodoError, match="another unresolved goal"):
        assign(todo_comms, "todo-2", generation="unrelated-order")
    assert TodoStore(todo_comms.root).get(second.id) == second
    assert todo_comms.registry.require("worker-a").require_goal().id == "order-a"
    assert first.assignment == UnassignedTodoAssignment()


def test_blocking_problem_retains_assignment_and_status_cannot_claim_delivery(todo_comms: Comms):
    create(todo_comms)
    reserved = assign(todo_comms)
    store = TodoStore(todo_comms.root)
    lead = todo_comms.registry.require("lead")
    blocked = store.set_state("todo-1", expected_revision=2, state=BlockedTodoState, actor=lead, goals=todo_comms.goals)
    assert blocked.assignment == reserved.assignment
    with pytest.raises(TodoConflict):
        store.transfer("todo-1", expected_revision=3, previous=reserved.assignment.require_assigned(reserved),
                       owner=todo_comms.registry.require("worker-b"), parent=lead, generation="order-b",
                       deadline=Deadline(time.time()+3600), goals=todo_comms.goals)
    opened = store.set_state("todo-1", expected_revision=3, state=OpenTodoState, actor=lead, goals=todo_comms.goals)
    with pytest.raises(TodoError, match="checked completion"):
        store.set_state("todo-1", expected_revision=4, state=DoneTodoState, actor=lead, goals=todo_comms.goals)
    assert store.get("todo-1") == opened


def test_real_acceptance_and_ordinary_goal_completion_close_same_problem(todo_comms: Comms):
    repo = Path(todo_comms.registry.require("lead").worktree)
    git(repo, "init", "-q")
    git(repo, "add", "problem.txt")
    git(repo, "commit", "-qm", "Retain unresolved problem")
    initial = git(repo, "rev-parse", "HEAD")
    create(todo_comms)
    assigned = assign(todo_comms)
    store = TodoStore(todo_comms.root)
    owner = todo_comms.registry.require("worker-a")
    with pytest.raises(TodoError, match="Acceptance expected 0"):
        store.complete("todo-1", expected_revision=2, actor=owner, progress=report(initial),
                       goals=todo_comms.goals, actor_kind=OwnerInvocable)
    assert store.get("todo-1") == assigned
    (repo / "problem.txt").write_text("Resolved.\nA real count is required.\n")
    git(repo, "add", "problem.txt")
    git(repo, "commit", "-qm", "Resolve counted problem")
    delivered = git(repo, "rev-parse", "HEAD")
    progress = report(delivered)
    todo_comms.goals.update_goal("worker-a", CompletedGoalAction(
        progress=progress, expect=GoalPrecondition(goal_id="order-a")), actor=OwnerInvocable)
    done = store.get("todo-1")
    assert done.state is DoneTodoState and done.revision == 3
    assert store.complete("todo-1", expected_revision=2, actor=owner, progress=progress,
                          goals=todo_comms.goals, actor_kind=OwnerInvocable) == done
    goal = todo_comms.registry.require("worker-a").require_goal()
    assert goal.delivery_time.timestamps == (int(git(repo, "show", "-s", "--format=%ct", "HEAD")),)
    assert goal.late
    messages = tuple(todo_comms.bus.log.full_history())
    assert len(messages) == 2 and all(message.target == "lead" for message in messages)
    assert "Works in app." in messages[-1].body


def test_goal_reference_never_rebinds_to_a_recreated_owner(todo_comms: Comms):
    create(todo_comms)
    reserved = assign(todo_comms)
    reference = reserved.assignment.require_assigned(reserved).goal
    current = todo_comms.registry.require("worker-a")
    todo_comms.registry.remove(current.name)
    todo_comms.registry.declare(Thread(current.name, current.tags, current.worktree,
                                       created_at=current.created_at + 100))
    todo_comms.goals.update_goal(current.name, SetGoalAction(text="An independent new order"),
                                actor=OwnerInvocable)
    replacement = todo_comms.registry.require(current.name).require_goal()
    assert reference.current(todo_comms.registry.snapshot()) is None
    with pytest.raises(ValueError, match="no longer current"):
        reference.require_current(todo_comms.registry)
    with todo_comms.goals.writing() as completed:
        ClearGoalAction.end_current(reference, goals=todo_comms.goals, completed=completed)
    assert todo_comms.registry.require("worker-a").require_goal() == replacement


def test_renames_preserve_problem_authority_and_order_identity(todo_comms: Comms):
    create(todo_comms)
    assigned = assign(todo_comms)
    previous = assigned.assignment.require_assigned(assigned)
    todo_comms.registry.rename("lead", "captain")
    todo_comms.registry.rename("worker-a", "unit")
    unit = todo_comms.registry.require("unit")
    reference = GoalRef(unit.incarnation.require_recorded(), "order-a")
    assert TodoRef("todo-1").require_current(todo_comms.root, reference) == assigned
    store = TodoStore(todo_comms.root)
    captain = todo_comms.registry.require("captain")
    store.set_state("todo-1", expected_revision=2, state=BlockedTodoState,
                    actor=captain, goals=todo_comms.goals)
    store.set_state("todo-1", expected_revision=3, state=OpenTodoState,
                    actor=unit, goals=todo_comms.goals)
    moved = store.transfer("todo-1", expected_revision=4, previous=previous,
        owner=todo_comms.registry.require("worker-b"), parent=captain,
        generation="after-rename", deadline=Deadline(time.time()+3600), goals=todo_comms.goals)
    assert moved.revision == 5 and todo_comms.registry.require("unit").goal is None
    assert todo_comms.registry.require("worker-b").require_goal().id == "after-rename"


def test_declared_problem_location_and_reference_validation(todo_comms: Comms):
    source = Path(todo_comms.registry.require("lead").worktree) / "problem.txt"
    with pytest.raises(TodoError):
        SourceTodoWhere(str(source), 99).require_source(todo_comms.bus)
    with pytest.raises(ValueError):
        GoalRef(RecordedThreadIncarnation("worker-a", float("nan")), "order-a")
    assignment = Assignment(GoalRef(todo_comms.registry.require("worker-a").incarnation, "order-a"),
                            todo_comms.registry.require("lead").incarnation)
    assert assignment.owner == assignment.goal.owner and assignment.generation == assignment.goal.goal_id


def test_fleet_uses_problem_assignment_and_exact_current_order(todo_comms: Comms):
    problem = create(todo_comms)
    store = TodoStore(todo_comms.root)
    initial, = store.fleet(todo_comms.registry, todo_comms.agents)
    assert isinstance(initial, UnassignedFleetEntry) and initial.todo == problem
    assigned = assign(todo_comms)
    todo_comms.agents.set_activity("worker-a", ActivityState.WORKING, "Inspecting the source")
    current, = store.fleet(todo_comms.registry, todo_comms.agents)
    assert isinstance(current, AssignedFleetEntry) and current.todo == assigned
    assert current.goal == todo_comms.registry.require("worker-a").require_goal()
    assert current.activity.state is ActivityState.WORKING
    assert FleetCliCommand().apply(todo_comms)["problems"][0]["todo"]["id"] == problem.id

    todo_comms.registry.rename("worker-a", "unit")
    renamed, = store.fleet(todo_comms.registry, todo_comms.agents)
    assert isinstance(renamed, AssignedFleetEntry) and renamed.goal.id == "order-a"
    old = todo_comms.registry.require("unit")
    todo_comms.registry.remove(old.name)
    todo_comms.registry.declare(Thread(old.name, old.tags, old.worktree, created_at=old.created_at + 100))
    former, = store.fleet(todo_comms.registry, todo_comms.agents)
    assert isinstance(former, FormerOrderFleetEntry) and former.todo == assigned
    assert former.todo.assignment.require_assigned(former.todo).goal == assigned.assignment.require_assigned(assigned).goal
