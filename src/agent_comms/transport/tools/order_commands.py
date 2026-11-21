from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Annotated

from pydantic import JsonValue

from agent_comms.nominal.field_codec import FieldCodec, OrderId
from agent_comms.planning.delivery_evidence import TodoProblems
from agent_comms.planning.goal_actions import CreateProblemGoalAction, OrderGoalAction
from agent_comms.planning.goals import Goal, OrderCheck
from agent_comms.planning.held_orders import HeldOrders
from agent_comms.planning.order_phase import OrderPhase
from agent_comms.planning.order_words import StrikeReason
from agent_comms.planning.orders import (
    CorrectCheckTodo,
    CorrectRunTodo,
    DeliverTodo,
    InstallTodo,
    MeasuredReview,
    OrderCompletion,
    OrderProgress,
    ProgressRead,
    ServeTodo,
    Todo,
    TodoOperation,
)
from agent_comms.planning.progress_claims import ProgressClaim
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.team.team_comms import TeamComms
from agent_comms.transport.request import tool_field
from agent_comms.transport.tool_caller import ThreadCaller, ToolCaller
from agent_comms.transport.tool_request import ThreadReadTool, ThreadToolRequest
from agent_comms.transport.tool_result import ToolResult


@dataclass(frozen=True)
class TodoChanged(ToolResult):
    todo: Todo
    strike_count: int


@dataclass(frozen=True)
class TodoList(ToolResult):
    todos: tuple[Todo, ...]
    progress: dict[str, OrderProgress] = field(default_factory=dict)


@dataclass(frozen=True)
class ProblemOrdered(ToolResult):
    goal: Goal
    todos: tuple[Todo, ...]


@dataclass(frozen=True)
class ProblemClosed(ToolResult):
    todo: Todo


@dataclass(frozen=True, kw_only=True)
class CommsTodoTool(ThreadToolRequest):
    label = "Change problem"
    description = (
        "Apply an exact revision-checked Todo command; only its teammate may invoke this tool."
    )
    problem: Annotated[str, *tool_field("Problem ID")]

    def apply(self, comms: TeamComms, caller: ThreadCaller) -> TodoChanged | ProblemClosed:
        todo = comms.goals.todos.get(self.problem)
        actor = caller.identity(comms)
        return self.result(comms.goals.change_problem(
            self.problem, self.operation_for(todo, actor, comms), actor))

    @abstractmethod
    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: TeamComms) -> TodoOperation:
        """Build the change, verifying any attached application claims before writing."""

    def result(self, changed: Todo) -> TodoChanged | ProblemClosed:
        return TodoChanged(changed, changed.strike_count)


@dataclass(frozen=True, kw_only=True)
class CommsChangeProblemTool(CommsTodoTool, declared_name="comms_todo"):
    operation: Annotated[
        TodoOperation, *tool_field("Typed assignment, strike, release or state command")
    ]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: TeamComms) -> TodoOperation:
        return self.operation.authored_by(actor)


@dataclass(frozen=True, kw_only=True)
class BoardReadTool(ThreadReadTool, declared_name=None):
    """A read of one teammate's board. An omitted thread reads the caller's own, and an
    empty board looks like a broken reader, so the reply names whose board it read."""

    def reply(self, read: dict[str, JsonValue], caller: ToolCaller) -> dict[str, JsonValue]:
        return {"board": caller.subject(self.thread), **read}


@dataclass(frozen=True, kw_only=True)
class CommsFireTool(ThreadToolRequest):
    label = "Fire teammate"
    description = (
        "Release every open problem the teammate holds in one operation, recording one "
        "plain reason on each; only their teammate may invoke this tool."
    )
    unit: Annotated[str, *tool_field("Teammate thread to fire")]
    reason: Annotated[StrikeReason, *tool_field("What the teammate did, plainly")]

    def apply(self, comms: TeamComms, caller: ThreadCaller) -> TodoList:
        return TodoList(comms.goals.fire(self.unit, self.reason, caller.identity(comms)))


@dataclass(frozen=True, kw_only=True)
class CommsTodoListTool(BoardReadTool):
    label = "Read problems"
    description = "Read the team's Todo orders, review timers, strikes and hunts."

    def apply(self, comms: TeamComms, caller: ToolCaller) -> TodoList:
        snapshot = comms.registry.snapshot()
        listed = tuple(comms.goals.todos.board())
        pulls = ProgressRead(listed)
        # An orphaned hold has no unit checkout to read; HolderGone reports it.
        held = HeldOrders.split(tuple(todo for todo in listed if todo.holder), snapshot)
        return TodoList(listed, {todo.id: OrderProgress.read(todo, comms.registry, pulls)
                                 for todo in held.todos})


@dataclass(frozen=True, kw_only=True)
class CommsOrderTool(ThreadToolRequest):
    label = "Assign problem order"
    description = (
        "Reserve the order's Todo assignments in one transaction and publish its Goal with "
        "the teammate's review timer; does not replay input."
    )
    order: Annotated[
        OrderGoalAction, *tool_field(
            "Problem, request, expected_revision, review_at, teammate, and together: the "
            "order's other open problems with their expected revisions")
    ]
    owner: Annotated[str, *tool_field("Assignee thread")]

    def apply(self, comms: TeamComms, caller: ThreadCaller) -> ProblemOrdered:
        order = self.order.bound_to(caller.identity(comms)).bound_in(
            comms.registry.snapshot().team_project)
        order = replace(order, review_at=order.review_at.continued_from(
            MeasuredReview.read(comms.goals.todos).review_at(order.order).at))
        goal = comms.goals.update_goal(self.owner, order)
        return ProblemOrdered(goal, tuple(comms.goals.todos.get(problem)
                                          for problem in self.order.problems()))


@dataclass(frozen=True, kw_only=True)
class CommsTodoCreateTool(CommsOrderTool):
    label = "Create problem"
    description = "Create a problem with its owner, checked order and review timer in one command."
    order: Annotated[
        CreateProblemGoalAction, *tool_field("Typed problem creation and order")
    ]


@dataclass(frozen=True, kw_only=True)
class CommsOrderDoneTool(CommsTodoTool):
    label = "Check delivered order"
    description = "Verify and deliver an open order by its own ID; the teammate's Goal is unchanged."
    order_id: Annotated[OrderId, *tool_field("Exact open order ID")]
    delivery: Annotated[
        OrderCompletion,
        *tool_field("A checked implementation delivery for fix, or a report for triage"),
    ]
    claims: Annotated[
        tuple[ProgressClaim, ...],
        *tool_field(
            "Typed evidence claims, each with the artifact Core verifies in the order's "
            "checkout; at least one for fix, none for triage reports"
        ),
    ]

    @classmethod
    def context_types(cls, comms: TeamComms, arguments: Mapping[str, object]) -> Mapping[str, type[object]]:
        problem = FieldCodec.field_plan(cls, "problem").read(arguments)
        return {"delivery": comms.goals.todos.get(problem).order.checked().scope.delivery_class}

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: TeamComms) -> TodoOperation:
        todo.require_maintainer(actor)
        todo.state.require_open(todo)
        order = todo.order.checked()
        order.require_delivery(self.order_id)
        problems = self.delivery.preparation_problems(todo)
        if problems:
            raise TodoProblems(problems)
        delivery = self.delivery.resolved_delivery(todo)
        required = order.scope.required_claims(delivery, self.claims, todo.id)
        # A problem claim is verified in that problem's own checkout, whatever worktree the
        # claimant's thread happens to hold: the scope is the claim's, not the caller's.
        verified = ProgressClaim.require_all(required, comms, comms.registry.require(actor.name))
        return DeliverTodo(todo.revision, actor, delivery, self.order_id, verified_claims=verified)


@dataclass(frozen=True, kw_only=True)
class CommsOrderCheckTool(CommsTodoTool):
    label = "Correct order check"
    description = (
        "Replace the check of an open order, recording the old check, the new one and "
        "why; only its teammate may invoke this tool, and the order keeps its review timer "
        "and strikes."
    )
    check: Annotated[
        OrderCheck, *tool_field("The corrected check: directory, command and expected result")
    ]
    why: Annotated[str, *tool_field("Why the recorded check was wrong")]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: TeamComms) -> TodoOperation:
        return CorrectCheckTodo(expected_revision=todo.revision, actor=actor, reason=self.why,
                                previous=todo.order.check, check=self.check)


@dataclass(frozen=True, kw_only=True)
class CommsOrderRunTool(CommsTodoTool):
    label = "Correct order run"
    description = (
        "Replace the run text that closes an open application order, recording the old run "
        "and why; only its teammate may invoke this tool, and the order keeps its review "
        "timer and strikes."
    )
    run: Annotated[str, *tool_field("The corrected run: what the installed app must show")]
    why: Annotated[str, *tool_field("Why the recorded run was wrong")]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: TeamComms) -> TodoOperation:
        previous = todo.order.checked().run
        return CorrectRunTodo(todo.revision, actor, self.why,
                              previous=previous, run=previous.retexted(self.run))


@dataclass(frozen=True, kw_only=True)
class CommsOrderServeTool(CommsTodoTool):
    label = "Name order phase"
    description = (
        "Name the plan phase an open order serves; only its teammate may invoke this "
        "tool, and the order keeps its id, review timer and strikes."
    )
    serves: Annotated[type[OrderPhase], *tool_field("The plan phase the order serves")]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: TeamComms) -> TodoOperation:
        return ServeTodo(todo.revision, actor, self.serves)


@dataclass(frozen=True, kw_only=True)
class CommsProblemCloseTool(CommsTodoTool):
    label = "Close installed problem"
    description = (
        "Record installed application evidence and close only a problem with no open order."
    )
    works: Annotated[
        str, *tool_field("What worked in the app, quoting the order's application run")
    ]
    artifact: Annotated[str, *tool_field("Existing verification artifact file")]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: TeamComms) -> TodoOperation:
        return InstallTodo(todo.revision, actor, self.works, self.artifact)

    def result(self, changed: Todo) -> ProblemClosed:
        return ProblemClosed(changed)
