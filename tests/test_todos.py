"""Problem closure and ordered delivery have distinct owners."""
from __future__ import annotations

from pathlib import Path
import subprocess
import time

import pytest

from agent_comms.comms import Comms
from agent_comms.goal_actions import CompletedGoalAction, GoalPrecondition, OwnerInvocable
from agent_comms.goal_progress import GoalReport, ReportedGoalProgress, WorksInApp
from agent_comms.goals import Deadline
from agent_comms.threads import Thread
from agent_comms.todos import (
    AcceptanceCheck, DoneTodoState, SourceTodoWhere, TodoConflict, TodoError,
    UnassignedTodoAssignment,
)


@pytest.fixture
def todo_comms(comms: Comms, tmp_path: Path) -> Comms:
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "problem.txt").write_text("P51_PENDING\nA real count is required.\n")
    for created, name in enumerate(("lead", "worker-a"), start=1):
        comms.registry.declare(Thread(name, frozenset(), str(repo), created_at=float(created)))
    return comms


def create(comms: Comms, todo_id: str = "todo-1"):
    lead = comms.registry.require("lead")
    repo = Path(lead.worktree)
    return comms.todos.create(
        todo_id, "Org/Repo", "Remove the pending marker", creator=lead,
        where=SourceTodoWhere(str(repo / "problem.txt"), 1),
        acceptance=AcceptanceCheck(str(repo),
            'python3 -c \'from pathlib import Path; print(Path("problem.txt").read_text().count("P51_PENDING"))\'', 0),
        bus=comms.bus,
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


def test_problem_closure_and_goal_owned_checked_delivery(todo_comms: Comms):
    store = todo_comms.todos
    lead = todo_comms.registry.require("lead")
    owner = todo_comms.registry.require("worker-a")
    unassigned = create(todo_comms, "unassigned")
    closed = store.set_state(unassigned.id, expected_revision=1, state=DoneTodoState,
                             actor=lead, goals=todo_comms.goals)
    assert closed.state is DoneTodoState and closed.assignment == UnassignedTodoAssignment()

    repo = Path(lead.worktree)
    git(repo, "init", "-q")
    git(repo, "add", "problem.txt")
    git(repo, "commit", "-qm", "Retain unresolved problem")
    initial = git(repo, "rev-parse", "HEAD")
    create(todo_comms)
    assigned = store.assign(
        "todo-1", expected_revision=1, owner=owner, parent=lead, generation="order-a",
        deadline=Deadline(time.time() - 60), goals=todo_comms.goals,
    )
    with pytest.raises(TodoError, match="checked completion"):
        store.set_state(assigned.id, expected_revision=2, state=DoneTodoState,
                        actor=lead, goals=todo_comms.goals)
    with pytest.raises(TodoError, match="Acceptance expected 0"):
        store.complete(assigned.id, expected_revision=2, actor=owner, progress=report(initial),
                       goals=todo_comms.goals, actor_kind=OwnerInvocable)
    assert store.get(assigned.id) == assigned

    (repo / "problem.txt").write_text("Resolved.\nA real count is required.\n")
    git(repo, "add", "problem.txt")
    git(repo, "commit", "-qm", "Resolve counted problem")
    progress = report(git(repo, "rev-parse", "HEAD"))
    todo_comms.goals.update_goal("worker-a", CompletedGoalAction(
        progress=progress, expect=GoalPrecondition(goal_id="order-a")), actor=OwnerInvocable)
    goal = todo_comms.registry.require("worker-a").require_goal()
    assert goal.delivery_complete and goal.late
    assert goal.delivery_time.timestamps == (int(git(repo, "show", "-s", "--format=%ct", "HEAD")),)
    assert store.get(assigned.id) == assigned
    assert store.complete(assigned.id, expected_revision=2, actor=owner, progress=progress,
                          goals=todo_comms.goals, actor_kind=OwnerInvocable) == assigned
    with pytest.raises(TodoConflict):
        store.release(assigned.id, expected_revision=2,
                      previous=assigned.assignment.require_assigned(assigned), goals=todo_comms.goals)
    row = next(row for row in store.fleet(todo_comms.registry, todo_comms.agents)
               if row.todo.id == assigned.id)
    assert row.todo == assigned and row.goal == goal
    messages = tuple(todo_comms.bus.log.full_history())
    assert len(messages) == 2 and all(message.target == "lead" for message in messages)
    assert "Works in app." in messages[-1].body
