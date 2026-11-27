"""Order-bearing goal actions linking Goal mutations with TodoStore writes.

Separated from goal_actions so read-only goal presentation and CLI readers do
not import orders.py, pull_requests.py, or head_review.py at startup.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import Annotated, Self

from agent_comms.nominal import field_codec
from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.attempt_resolution import OrderDecision
from agent_comms.planning.goal_actions import (
    GoalAction,
    GoalActionContext,
    RuntimeInvocable,
    SetGoalAction,
)
from agent_comms.planning.goal_mentions import bind_goal_mentions
from agent_comms.planning.goal_mutation import GoalStateOrder
from agent_comms.planning.goal_states import ActiveGoal
from agent_comms.planning.goals import Goal, ProblemGoalOrder
from agent_comms.planning.orders import (
    PendingProblemOrder,
    RecordOrderHeadVerdict,
    SendBackVerdict,
    Todo,
    TodoCommand,
)
from agent_comms.planning.owner_requests import OwnerRequestId
from agent_comms.planning.problem_findings import ExpectedProblem
from agent_comms.projects.change_report import CitationProject, FileLine, NoSourceReference
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.registry.registry_document import TeamProject
from agent_comms.registry.thread_identity import CoreWatcherIncarnation, ThreadIncarnation
from agent_comms.registry.threads import Thread
from agent_comms.storage.recorded_time import EventTime


@dataclass(frozen=True, kw_only=True)
class HeldOrderGoalAction(GoalAction[Goal], RuntimeInvocable):
    """A Todo operation on a held order and the revision of the holder's Goal, in one write."""
    problem: str
    operation: TodoCommand

    def revised(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        return goal

    def prepare(self, goals, name, actor):
        ctx = GoalActionContext(goals, goals.registry.require(name), actor)
        self.expect.check(ctx)
        goal = ctx.thread.goal.require()
        todo = goals.todos.get(self.problem)
        todo.require_assignee(ctx.thread.incarnation, goal.id)
        operation = self.operation.prepare(goals, todo)
        return replace(self, operation=operation, expect=replace(self.expect, expected_checkpoint=goal.captured))

    def change(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        ctx.goals.todos.apply(self.problem, self.operation)
        return replace(self.revised(goal, ctx), revision=goal.revision + 1)


@dataclass(frozen=True, kw_only=True)
class SentBackOrderGoalAction(HeldOrderGoalAction):
    operation: SendBackVerdict


@dataclass(frozen=True, kw_only=True)
class HeadReviewedOrderGoalAction(HeldOrderGoalAction):
    operation: RecordOrderHeadVerdict


@dataclass(frozen=True, kw_only=True)
class ProblemOrder:
    """The problem a goal action or task is about and the order it belongs to."""

    problem: str
    order: PendingProblemOrder


@dataclass(frozen=True, kw_only=True)
class ProblemOrderGoalAction(
    ProblemOrder,
    GoalAction[Goal],
    RuntimeInvocable,
    GoalStateOrder[Goal, GoalActionContext],
):
    request: OwnerRequestId
    review_at: EventTime
    assigner: ThreadIncarnation
    """Who sent the order: the teammate, or a fork working inside its turn."""

    def bound_to(self, actor: ThreadIncarnation) -> ProblemOrderGoalAction:
        return replace(self, assigner=actor)

    def bound_in(self, project: CitationProject) -> ProblemOrderGoalAction:
        return replace(self, order=self.order.bound_in(project))

    def sender_teammate(self, ctx: GoalActionContext) -> Thread:
        """The teammate whose authority sends the order, read from the registry: the sender
        itself, or the teammate whose turn a fork works inside. A created problem records it
        as creator and the request must be its own."""
        sender = ctx.goals.registry.require(self.assigner.name)
        return ctx.goals.registry.require(sender.teammate())

    def authority(self, ctx: GoalActionContext) -> ThreadIncarnation:
        """Whose open request the order may serve: its sender teammate's."""
        return self.sender_teammate(ctx).acting_identity

    @abstractmethod
    def reserve(self, ctx: GoalActionContext, generation: GoalId) -> None:
        """Write this order's Todos under one Todo transaction, inside this goal update."""

    def problems(self) -> tuple[str, ...]:
        return (self.problem,)

    def change(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        return goal.state.accept_order(self, goal, ctx)

    def reactivate_goal(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        return replace(self.continue_goal(goal, ctx), state=ActiveGoal())

    def replace_goal(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        goal.order.require_replacement(goal, ctx.goals, ctx.thread.incarnation)
        return self.create(ctx)

    def continue_goal(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        self.require_ready(ctx)
        self.reserve(ctx, goal.id)
        store = ctx.goals.open_goal_store()
        generation = store.require_generation(goal.id)
        generation.lifecycle.continue_work(store, generation, OrderDecision(self.order.id))
        text = ProblemGoalOrder.derived_text(
            ctx.goals.todos.for_goal(ctx.thread.incarnation, goal.id)
        )
        revision = goal.revision + 1
        return replace(goal, order=ProblemGoalOrder(), text=text, progress="",
                       revision=revision, mention_source=bind_goal_mentions(
                           text, goal.id, revision, ctx.thread,
                           ctx.goals.registry.snapshot()))

    def require_ready(self, ctx: GoalActionContext) -> None:
        """Recheck local request facts before reserving work or a child; the Todo write that
        reserves the work decides holder and goal consent."""
        self.require_sendable(ctx.goals.registry.snapshot().team_project)
        ctx.goals.todos.owner_request(self.request).require_open(self.authority(ctx))
        self.review_at.require_recorded("A review time")

    def require_sendable(self, project: TeamProject) -> None:
        """A new order's wording and source references hold at its declared revision."""
        self.order.require_wording()
        self.order.require_references(project)
        self.order.check.require_directory()

    def create(self, ctx: GoalActionContext) -> Goal:
        self.require_ready(ctx)
        goal_id = GoalId.mint()
        self.reserve(ctx, goal_id)
        text = ProblemGoalOrder.derived_text(
            ctx.goals.todos.for_goal(ctx.thread.incarnation, goal_id)
        )
        return SetGoalAction(text=text, order=ProblemGoalOrder(), goal_id=goal_id).create(ctx)


@dataclass(frozen=True, kw_only=True)
class OrderGoalAction(ProblemOrderGoalAction):
    """The order's teammate gives the problem, and the order's other open problems, to this
    thread: one goal update and one Todo transaction for the whole group."""

    expected_revision: int
    traced: Annotated[FileLine | NoSourceReference, field_codec.WireOmitDefault()] = (
        NoSourceReference()
    )
    together: Annotated[tuple[ExpectedProblem, ...], field_codec.WireOmitDefault()] = ()

    def expected(self) -> tuple[ExpectedProblem, ...]:
        return (ExpectedProblem(self.problem, self.expected_revision), *self.together)

    def problems(self) -> tuple[str, ...]:
        return tuple(expected.problem for expected in self.expected())

    def reserve(self, ctx: GoalActionContext, generation: GoalId) -> None:
        ctx.goals.todos.assign(self.expected(),
            owner=ctx.thread, assigner=self.assigner,
            generation=generation, request=self.request, review_at=self.review_at, order=self.order,
            traced=self.traced)


@dataclass(frozen=True, kw_only=True)
class ReissuedOrderGoalAction(OrderGoalAction):
    """Core hands a holder an order its Todo already accepted.

    The order was sendable when accepted; its unit has edited the sources it names
    since, so they are not checked again against the moved tree.
    """

    def authority(self, ctx: GoalActionContext) -> ThreadIncarnation:
        """Core reissues under its own identity; no teammate's turn sends it."""
        return self.assigner

    @classmethod
    def for_todo(cls, todo: Todo) -> Self:
        """Core reissues accepted unheld work, recording itself rather than an invented actor."""
        now = time.time()
        order = todo.order.reissued(now)
        return cls(
            problem=todo.id, expected_revision=todo.revision,
            order=order, request=todo.request.require(),
            review_at=todo.reissued_review(now),
            assigner=CoreWatcherIncarnation.watcher(),
            traced=order.owner)

    def require_sendable(self, project: TeamProject) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class FiledProblem:
    """The repository a problem is filed in and its authored text."""

    repo: RepositoryName
    text: str


@dataclass(frozen=True, kw_only=True)
class CreateProblemGoalAction(FiledProblem, ProblemOrderGoalAction):
    def reserve(self, ctx: GoalActionContext, generation: GoalId) -> None:
        ctx.goals.todos.create(self.problem, self.repo, self.text,
            creator=self.sender_teammate(ctx), owner=ctx.thread,
            assigner=self.assigner, generation=generation, request=self.request,
            review_at=self.review_at, order=self.order)
