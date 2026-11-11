"""Real assignment history keeps a finished problem Goal addressable."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_comms.cli_verbs.cli_commands import ReconciledStoresResult
from agent_comms.comms import Comms
from agent_comms.errors import GoalRefused
from agent_comms.nominal.field_codec import FieldCodec, GoalId, OrderId
from agent_comms.planning.goal_actions import (
    CompletedGoalAction,
    CreateProblemGoalAction,
    GoalPrecondition,
    OrderGoalAction,
)
from agent_comms.planning.goal_scope import OneGoal
from agent_comms.planning.goal_states import CompletedGoal
from agent_comms.planning.goals import Goal
from agent_comms.planning.order_words import StrikeReason
from agent_comms.planning.orders import (
    AssignTodo,
    DeliverTodo,
    InstallTodo,
    OperationDelivery,
    ReleaseTodo,
    StoredOrderStory,
    Todo,
    TodoStore,
)
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.threads import NativeThread, Thread
from agent_comms.storage.recorded_time import RecordedEventTime
from conftest import installed_comms, maintenance_held
from source_revision_fixture import SourceRepository
from test_reconcile_stores import reconcile
from test_todos import order


@dataclass
class AssignedProblems:
    comms: Comms
    source: SourceRepository
    owner: Thread
    goal: Goal
    artifact: Path

    @property
    def store(self) -> TodoStore:
        return self.comms.goals.todos

    def close(self, problem: str, owner: Thread | None = None) -> Todo:
        actor = (owner or self.owner).incarnation
        current = self.store.get(problem)
        delivered = self.store.apply(problem, DeliverTodo(
            current.revision, actor, OperationDelivery(at=RecordedEventTime(30)),
            current.order.id,
        ))
        return self.store.apply(problem, InstallTodo(
            delivered.revision, actor,
            "The isolated problem's requested application behavior worked.", str(self.artifact),
        ))

    def complete(self) -> Goal:
        return self.comms.goals.update_goal(self.owner.name, CompletedGoalAction(
            expect=GoalPrecondition(goal_id=OneGoal(self.goal.id)),
        )).require()


@pytest.fixture
def assigned_problems(tmp_path: Path, source_repository: SourceRepository) -> AssignedProblems:
    comms = installed_comms(tmp_path / "wire")
    comms.registry.set_team_project(source_repository.project)
    comms.registry.declare(NativeThread(
        "worker", frozenset(), Checkout(source_repository.checkout),
    ))
    owner = comms.registry.require("worker")
    request = comms.goals.todos.add_request(owner.incarnation, "Finish the isolated problem")
    goal = comms.goals.update_goal(owner.name, CreateProblemGoalAction(
        problem="P1", repo=source_repository.github, text="Fix the isolated problem",
        order=order(source_repository), request=request.id,
        review_at=RecordedEventTime(1000), assigner=owner.incarnation,
    )).require()
    artifact = tmp_path / "application.txt"
    artifact.write_text("The isolated application's requested behavior worked.\n")
    return AssignedProblems(comms, source_repository, owner, goal, artifact)


def test_an_open_problem_keeps_the_goal_from_completing(assigned_problems: AssignedProblems) -> None:
    with pytest.raises(GoalRefused, match="Complete each problem"):
        assigned_problems.complete()


def test_closed_assignments_preserve_exact_goal_membership(assigned_problems: AssignedProblems) -> None:
    scene = assigned_problems
    closed = scene.close("P1")
    assert closed.assignment is None
    assert not scene.store.for_goal(scene.owner.incarnation, scene.goal.id)
    completed = scene.complete()
    assert tuple(todo.id for todo in scene.store.closed_for_goal(
        scene.owner.incarnation, scene.goal.id,
    )) == (closed.id,)
    assert completed.id == scene.goal.id
    assert isinstance(completed.state, CompletedGoal)
    assert scene.store.get(closed.id) == closed


def test_unrelated_goal_and_reused_owner_birth_do_not_gain_membership(
    assigned_problems: AssignedProblems,
) -> None:
    scene = assigned_problems
    scene.close("P1")
    assert not scene.store.closed_for_goal(scene.owner.incarnation, GoalId.mint())
    reused = RecordedThreadIncarnation(scene.owner.name, scene.owner.created_at + 1)
    assert not scene.store.closed_for_goal(reused, scene.goal.id)


def test_owner_and_goal_must_come_from_the_same_historical_assignment(
    assigned_problems: AssignedProblems,
) -> None:
    scene = assigned_problems
    first = scene.store.get("P1")
    released = scene.store.apply(first.id, ReleaseTodo(
        first.revision, scene.owner.incarnation, first.held(),
        StrikeReason("The first owner released this isolated problem for another independent Goal."),
    ))
    scene.comms.registry.declare(NativeThread(
        "other", frozenset(), Checkout(scene.source.checkout),
    ))
    other = scene.comms.registry.require("other")
    next_goal = scene.comms.goals.update_goal(other.name, OrderGoalAction(
        problem=first.id, expected_revision=released.revision,
        order=replace(order(scene.source), id=OrderId.mint()), request=first.request.require(),
        review_at=RecordedEventTime(1000), assigner=scene.owner.incarnation,
    )).require()
    scene.close(first.id, other)
    assert tuple(todo.id for todo in scene.store.closed_for_goal(
        scene.owner.incarnation, scene.goal.id,
    )) == (first.id,)
    assert tuple(todo.id for todo in scene.store.closed_for_goal(
        other.incarnation, next_goal.id,
    )) == (first.id,)
    assert not scene.store.closed_for_goal(scene.owner.incarnation, next_goal.id)
    assert not scene.store.closed_for_goal(other.incarnation, scene.goal.id)
    assert len(scene.store.events(first.id, ReleaseTodo)) == 2


def test_stale_merge_records_neither_row_nor_assignment_history(
    assigned_problems: AssignedProblems,
) -> None:
    scene = assigned_problems
    current = scene.store.get("P1")
    observed = current.order.run.after_merge(
        current, current.order.merged(scene.source.main_commit),
    )
    corrected = scene.store.apply(current.id, AssignTodo(
        expected_revision=current.revision, actor=scene.owner.incarnation,
        proposed=current.held(), review_at=RecordedEventTime(1001),
        order=current.order, request=current.request.require(),
    ))
    assert corrected.assignment == current.assignment
    assert corrected.revision > current.revision
    history = scene.store.sources(current.id)
    scene.store.record_merges((observed,))
    assert scene.store.get(current.id) == corrected
    assert scene.store.sources(current.id) == history
    assert not scene.store.events(current.id, ReleaseTodo)


def test_historical_membership_uses_its_index(assigned_problems: AssignedProblems) -> None:
    scene = assigned_problems
    closed = scene.close("P1")
    assert len(scene.store.events(closed.id, ReleaseTodo)) == 1
    statements: list[str] = []
    with scene.store.snapshot() as db:
        db.set_trace_callback(statements.append)
        scene.store.goal_members_in(
            db, "1", (), unit=scene.owner.incarnation, goal=scene.goal.id,
        )
        db.set_trace_callback(None)
        statement, = statements
        plan = "\n".join(str(tuple(row)) for row in db.execute("EXPLAIN QUERY PLAN " + statement))
    assert StoredOrderStory.assignment_index.name(StoredOrderStory) in plan


def test_reconcile_retains_closed_membership_on_the_previous_schema(
    assigned_problems: AssignedProblems,
) -> None:
    scene = assigned_problems
    closed = scene.close("P1")
    history = scene.store.events(closed.id, ReleaseTodo)
    # This isolated bus has the preceding runtime's schema, without the new index.
    with closing(sqlite3.connect(scene.store.path)) as db:
        db.execute(f'DROP INDEX "{StoredOrderStory.assignment_index.name(StoredOrderStory)}"')
        db.commit()
    with maintenance_held(scene.store.root) as holder:
        result = reconcile(scene.store.root, holder)
    assert result.returncode == 0, result.stdout + result.stderr
    carried = FieldCodec.decode(ReconciledStoresResult, FieldCodec.parse(result.stdout))
    assert Path(scene.store.filename) in carried.rewritten
    assert scene.store.events(closed.id, ReleaseTodo) == history
    assert scene.store.get(closed.id) == closed
    assert isinstance(scene.complete().state, CompletedGoal)
