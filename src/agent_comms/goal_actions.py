"""Goal commands own behavior; one parent owns CAS, publication and actor checks."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar
from uuid import uuid4

from .child_process import ProcessIdentity
from .command import Command
from .declared_family import DeclaredFamily
from .goal_mentions import bind_goal_mentions
from .goal_states import (
    ActiveGoal,
    BlockedGoal,
    BlockedState,
    CompletedGoal,
    GoalState,
    OwnerPause,
    PausedGoal,
    RuntimePause,
)
from .goal_waits import GoalWaits, RecordedGoalWait
from .goals import Goal, GoalDeclaration, GoalOrder, NoGoal, ProblemGoalOrder, UnorderedGoalOrder
from .recorded_time import EventTime
from .threads import Thread
from .todos import DeliverTodo, PendingProblemOrder

if TYPE_CHECKING:
    from .goal_management import Goals
    from .goal_presentation import GoalSnapshot


class ModelInvocable:
    pass


class OwnerInvocable:
    pass


class RuntimeInvocable:
    pass


class OwnerControlInvocable:
    pass


@dataclass(frozen=True)
class GoalPrecondition:
    goal_id: str | None = None
    expected_state: GoalState | None = None
    expected_goal: GoalDeclaration = NoGoal()
    expected_owner: ProcessIdentity | None = None

    def check(self, ctx: GoalActionContext) -> None:
        thread, goal = ctx.thread, ctx.thread.goal
        if self.expected_owner is not None and (
            thread.process_identity != self.expected_owner
            or not ctx.goals.registry.status(thread.name).running
        ):
            raise ValueError("The goal owner changed; refresh its state.")
        self.expected_goal.check_expected(goal)
        if self.goal_id is not None:
            thread.goal.require_id(self.goal_id)
        if self.expected_state is not None and (
            not goal.is_set() or goal.state != self.expected_state
        ):
            raise ValueError(
                goal.state.activity_refusal() if goal.is_set()
                else "This goal is no longer active; refresh its state."
            )


@dataclass(frozen=True)
class GoalActionContext:
    goals: Goals
    thread: Thread
    actor: type = RuntimeInvocable

    @property
    def report_turn(self) -> str:
        return self.thread.turn_record.read().report_turn

    @property
    def model_report(self) -> bool:
        return self.actor is ModelInvocable



@dataclass(frozen=True, kw_only=True)
class GoalAction(DeclaredFamily, Command, affix="GoalAction"):
    expect: GoalPrecondition = GoalPrecondition()
    preserve_wait: ClassVar[bool] = False
    schedules_goal: ClassVar[bool] = False

    @classmethod
    def model_choices(cls) -> tuple[str, ...]:
        return tuple(member.require_name() for member in cls.members_with(ModelInvocable))

    @classmethod
    async def submit_control(
        cls,
        observe: Callable[[], Awaitable[GoalSnapshot]],
        invoke: Callable[..., Awaitable[dict]],
        text: str = "",
    ) -> dict:
        goal = (await observe()).goal
        if not goal.is_set():
            raise ValueError("The goal changed; refresh its state.")
        return await cls.submit_checkpoint(invoke, goal)

    @classmethod
    async def submit_checkpoint(cls, invoke: Callable[..., Awaitable[dict]], goal: Goal) -> dict:
        from .field_codec import FieldCodec
        from .runtime_requests import UpdateGoalRuntimeRequest

        return await invoke(
            FieldCodec.encode(UpdateGoalRuntimeRequest), status=cls,
            goal_id=goal.id, expected_revision=goal.revision,
        )

    def require_actor(self, actor: type) -> None:
        if not isinstance(self, actor):
            raise ValueError(f"This actor cannot take goal action {self.declared_name!r}.")

    def prepare(self, goals: Goals, name: str, actor: type) -> GoalAction:
        return self

    def apply(self, ctx: GoalActionContext) -> GoalDeclaration:
        self.expect.check(ctx)
        self.require_actor(ctx.actor)
        if ctx.model_report and any(
            entry.reports_turn(ctx.report_turn)
            for entry in ctx.goals.registry.goal_history(ctx.thread.name)
        ):
            raise ValueError("This goal was already reported in this turn.")
        goal = self.change(ctx)
        if goal == ctx.thread.goal:
            return goal
        self.before_publish(goal, ctx)
        ctx.goals.registry.register(
            replace(
                ctx.thread,
                goal=goal,
            ),
            ctx.goals.registry.status(ctx.thread.name),
        )
        if not self.preserve_wait and ctx.thread.goal.is_set():
            ctx.goals.waits.clear(ctx.thread.goal.id)
        return goal

    @abstractmethod
    def change(self, ctx: GoalActionContext) -> GoalDeclaration: ...

    def before_publish(self, goal: GoalDeclaration, ctx: GoalActionContext) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class TransitionGoalAction(GoalAction):
    progress: str | None = None

    @abstractmethod
    def next_state(self, ctx: GoalActionContext) -> GoalState: ...

    def change(self, ctx: GoalActionContext) -> Goal:
        goal = ctx.thread.goal.require()
        goal.require_order(ctx.goals, ctx.thread.incarnation)
        state = self.next_state(ctx)
        goal.state.check_transition(state, owner=ctx.actor is OwnerInvocable)
        self.before_transition(ctx)
        return replace(
            goal,
            state=state,
            revision=goal.revision + 1,
            progress=goal.progress if self.progress is None else self.progress,
            reported_turn=ctx.report_turn if ctx.model_report else goal.reported_turn,
        )

    def before_transition(self, ctx: GoalActionContext) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class ActiveGoalAction(
    TransitionGoalAction, ModelInvocable, OwnerInvocable, RuntimeInvocable, OwnerControlInvocable
):
    schedules_goal = True

    def next_state(self, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def before_transition(self, ctx: GoalActionContext) -> None:
        goal, thread = ctx.thread.goal.require(), ctx.thread
        generation = ctx.goals.open_goal_store().require_generation(goal.id)
        if generation.lifecycle.failed and generation.attempt_id:
            # A status-only Resume cannot authorize an uncertain attempt.
            refusal = required_block_reason(
                "The interrupted goal attempt is unresolved. Inspect it, then use "
                "Retry to authorize a new attempt. Your messages can still be sent."
            )
            blocked = replace(
                goal,
                state=BlockedGoal(refusal),
                progress=goal.progress,
                revision=goal.revision + 1,
            )
            ctx.goals.registry.register(
                replace(thread, goal=blocked), ctx.goals.registry.status(thread.name)
            )
            raise ValueError(refusal)
        elif not generation.lifecycle.allows_resume(thread.turn_record.read().executing):
            raise ValueError("The goal attempt is unresolved; inspect it before Retry.")


@dataclass(frozen=True, kw_only=True)
class StandbyGoalAction(TransitionGoalAction, ModelInvocable, RuntimeInvocable):
    wait_for: tuple[str, ...] = ()
    reviewed_inputs: tuple[str, ...] = ()
    preserve_wait = True

    def next_state(self, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def before_publish(self, goal: GoalDeclaration, ctx: GoalActionContext) -> None:
        assert goal
        thread = ctx.thread
        report_turn = ctx.report_turn
        review = ctx.goals._goal_input_review(thread, goal.id, self.wait_for)
        wait_targets = review.targets
        from .input_disposition import InputDispositions

        aliases = review.owners
        dispositions = InputDispositions(ctx.goals.root)
        unknown = {row.key: row for row in review.unknown}
        reviewed_keys = tuple(dict.fromkeys(self.reviewed_inputs))
        if any(key not in unknown or unknown[key].sequence is None for key in reviewed_keys):
            raise ValueError("Review only this recipient's exact unresolved bus input keys.")
        if not set(reviewed_keys) <= review.eligible_keys:
            raise ValueError("Review only direct inputs from these declared dependencies.")
        pending = next(
            (
                row
                for row in review.unknown
                if row.key in review.eligible_keys
                and row.key not in reviewed_keys
                and not row.reviewed_for_goal(goal.id)
            ),
            None,
        )
        if pending is not None:
            sequence = pending.sequence
            raise ValueError(
                f"Dependency reply {sequence} is already pending or UNKNOWN. "
                f"Call comms_inbox with goal_id={goal.id!r} and "
                f"wait_for={list(self.wait_for)!r}. Inspect standby_review.messages, then "
                "pass only standby_review.reviewed_inputs to comms_goal to wait "
                "for a later reply. Do not pass excluded owner or other dependency inputs. "
                "This does not mark them STARTED or replay them."
            )
        snapshot = ctx.goals.registry.snapshot()
        if not any(
            ctx.goals.agents.is_working(target.incarnation, snapshot)
            for target in wait_targets
        ):
            names = ", ".join(f"@{target.incarnation.name}" for target in wait_targets)
            raise ValueError(
                f"No declared dependency is working ({names}). "
                "A running/ready process or queued input does not prove active work. "
                "Message or restart the responsible agent, inspect its status, "
                "then declare standby only while a target is actually working."
            )
        closed = GoalWaits.closed_wait_group(
            thread.name,
            wait_targets,
            ctx.goals.waits.read(),
            snapshot,
            ctx.goals.agents,
        )
        if closed:
            names = ", ".join(f"@{name}" for name in closed)
            raise ValueError(
                f"Standby would close a dependency wait group ({names}). "
                "At least one agent must remain able to work or reply. "
                "Continue independent work or change the dependencies."
            )
        dispositions.review_for_goal(
            reviewed_keys,
            observed=tuple(unknown[key] for key in reviewed_keys),
            owners=aliases,
            goal_id=goal.id,
            goal_revision=goal.revision,
            turn_id=report_turn,
        )
        # Commit scheduling authority first. A crash before the registry
        # progress update must leave this same goal waiting, not runnable.
        reporting_lease = thread.turn_lease
        ctx.goals.waits.record(
            RecordedGoalWait(
                goal.id,
                uuid4().hex,
                goal.revision,
                ctx.goals.bus.log.latest_sequence(),
                wait_targets,
                owner=thread.incarnation.require_recorded(),
                report_turn_id=reporting_lease.turn_id if reporting_lease is not None else None,
                report_turn_generation=(
                    reporting_lease.identity.generation if reporting_lease is not None else None
                ),
                target_turn_generations=tuple(
                    (
                        lease.identity.generation
                        if (lease := snapshot.threads[
                            snapshot.canonical_name(target.incarnation.name)
                        ].turn_lease) is not None
                        else None
                    )
                    for target in wait_targets
                ),
            )
        )


@dataclass(frozen=True, kw_only=True)
class CompletionGoalAction(TransitionGoalAction):
    def next_state(self, ctx: GoalActionContext) -> GoalState:
        return CompletedGoal()


@dataclass(frozen=True, kw_only=True)
class CompletedGoalAction(CompletionGoalAction, ModelInvocable, OwnerInvocable, RuntimeInvocable):
    def before_transition(self, ctx):
        ctx.thread.goal.require().order.require_completion()


@dataclass(frozen=True, kw_only=True)
class DeliveredOrderGoalAction(GoalAction, RuntimeInvocable):
    problem: str
    operation: DeliverTodo
    progress: str = ""

    def prepare(self, goals, name, actor):
        ctx = GoalActionContext(goals, goals.registry.require(name), actor)
        self.expect.check(ctx)
        goal = ctx.thread.goal.require()
        todo = goals.todos.get(self.problem)
        todo.require_assignee(ctx.thread.incarnation, goal.id)
        operation = self.operation.prepare(goals, todo)
        return replace(self, operation=operation, expect=replace(self.expect, expected_goal=goal))

    def change(self, ctx):
        ctx.goals.todos.apply(self.problem, self.operation)
        goal = ctx.thread.goal.require()
        pending = any(todo.pending for todo in ctx.goals.todos.for_goal(ctx.thread.incarnation, goal.id))
        return replace(goal, state=goal.state.after_order_delivery(pending),
                       progress=self.progress, revision=goal.revision + 1)


@dataclass(frozen=True, kw_only=True)
class BlockedGoalAction(TransitionGoalAction, ModelInvocable, OwnerInvocable, RuntimeInvocable):
    block_reason: str | None = None

    def next_state(self, ctx: GoalActionContext) -> GoalState:
        return BlockedGoal(
            required_block_reason(
                self.block_reason if self.block_reason is not None else self.progress
            )
        )


@dataclass(frozen=True, kw_only=True)
class PausedGoalAction(
    TransitionGoalAction, OwnerInvocable, RuntimeInvocable, OwnerControlInvocable
):
    def next_state(self, ctx: GoalActionContext) -> GoalState:
        current = ctx.thread.goal.require().state.pause_source
        return PausedGoal(
            current or (OwnerPause() if ctx.actor is OwnerInvocable else RuntimePause())
        )


@dataclass(frozen=True, kw_only=True)
class ReplacementGoalAction(GoalAction):
    def before_publish(self, goal: GoalDeclaration, ctx: GoalActionContext) -> None:
        goal = ctx.thread.goal
        if goal.is_set():
            # Revoke a protected goal before removing or replacing its
            # registry identity. If the registry write then fails, the
            # remaining visible goal is safely unlaunchable.
            attempts = ctx.goals.open_goal_store()
            generation = attempts.snapshot(goal.id)
            if generation is not None and not generation.lifecycle.terminal:
                attempts.retire_goal(
                    goal.id,
                    expected_generation=generation.number,
                    attempt_id=generation.attempt_id,
                )


@dataclass(frozen=True, kw_only=True)
class SetGoalAction(ReplacementGoalAction, OwnerInvocable, RuntimeInvocable):
    text: str
    order: GoalOrder = field(default_factory=UnorderedGoalOrder)
    goal_id: str = field(default_factory=lambda: uuid4().hex)

    @classmethod
    async def submit_control(cls, observe, invoke, text: str = "") -> dict:
        from .field_codec import FieldCodec
        from .runtime_requests import SetGoalRuntimeRequest

        return await invoke(FieldCodec.encode(SetGoalRuntimeRequest), text=text)

    def change(self, ctx: GoalActionContext) -> Goal:
        current = ctx.thread.goal.matching(self.goal_id)
        if current.is_set():
            if (current.text, current.order) != (self.text.strip(), self.order):
                raise ValueError("Goal identity already has different instructions.")
            return ctx.thread.goal.require()
        ctx.thread.goal.require_replacement(ctx.goals, ctx.thread.incarnation)
        goal = Goal(
            text=self.text.strip(), id=self.goal_id, revision=1, order=self.order,
            published_seq=ctx.goals.bus.log.latest_sequence(),
        )
        goal = replace(
            goal,
            mention_source=bind_goal_mentions(
                goal.text, goal.id, goal.revision, ctx.thread, ctx.goals.registry.snapshot()
            ),
        )
        ctx.goals.open_goal_store().create_goal(goal.id)
        return goal


@dataclass(frozen=True, kw_only=True)
class ProblemOrderGoalAction(GoalAction, RuntimeInvocable):
    problem: str
    order: PendingProblemOrder
    goal_id: str
    deadline: EventTime
    commander: str

    @abstractmethod
    def reserve(self, ctx): ...

    def change(self, ctx):
        ctx.thread.goal.require_order_slot(self.goal_id)
        self.deadline.require_deadline()
        self.order.owner.require_exists()
        self.order.check.require_directory()
        todo = self.reserve(ctx)
        current = ctx.thread.goal.matching(self.goal_id)
        if current.is_set():
            return replace(current, order=ProblemGoalOrder(), revision=current.revision + 1)
        return SetGoalAction(text="Complete the assigned problems.", goal_id=self.goal_id, order=ProblemGoalOrder()).change(ctx)


@dataclass(frozen=True, kw_only=True)
class OrderGoalAction(ProblemOrderGoalAction):
    expected_revision: int

    def reserve(self, ctx):
        return ctx.goals.todos.assign(self.problem, expected_revision=self.expected_revision,
            owner=ctx.thread, parent=ctx.goals.registry.require(self.commander),
            generation=self.goal_id, deadline=self.deadline, order=self.order)


@dataclass(frozen=True, kw_only=True)
class CreateProblemGoalAction(ProblemOrderGoalAction):
    repo: str
    text: str

    def reserve(self, ctx):
        return ctx.goals.todos.create(self.problem, self.repo, self.text,
            creator=ctx.goals.registry.require(self.commander), owner=ctx.thread,
            generation=self.goal_id, deadline=self.deadline, order=self.order)


@dataclass(frozen=True, kw_only=True)
class ClearGoalAction(
    ReplacementGoalAction, OwnerInvocable, RuntimeInvocable, OwnerControlInvocable
):
    def change(self, ctx: GoalActionContext) -> GoalDeclaration:
        ctx.thread.goal.require_replacement(ctx.goals, ctx.thread.incarnation)
        return NoGoal()


@dataclass(frozen=True, kw_only=True)
class EditGoalAction(GoalAction, OwnerInvocable, RuntimeInvocable):
    text: str
    preserve_wait = True

    def change(self, ctx: GoalActionContext) -> Goal:
        goal = ctx.thread.goal.require()
        ctx.thread.goal.require_replacement(ctx.goals, ctx.thread.incarnation)
        text = self.text.strip()
        if not text:
            raise ValueError("A goal requires text.")
        revision = goal.revision + 1
        return replace(
            goal,
            text=text,
            revision=revision,
            mention_source=bind_goal_mentions(
                text, goal.id, revision, ctx.thread, ctx.goals.registry.snapshot()
            ),
        )


def required_block_reason(reason: str | None) -> str:
    """Validate the block's own reason, independently of prior progress."""
    if type(reason) is not str or not reason.strip():
        raise ValueError("Blocking a goal requires a nonempty reason for the needed input.")
    normalized = reason.strip()
    if len(normalized) > 1024:
        raise ValueError("A blocked-goal reason must be at most 1024 characters.")
    return normalized


@dataclass(frozen=True, kw_only=True)
class RetryGoalAction(GoalAction, OwnerInvocable):
    """Explicit owner retry; existing store grants still fence every attempt."""

    @classmethod
    async def submit_checkpoint(cls, invoke, goal: Goal) -> dict:
        from .field_codec import FieldCodec
        from .runtime_requests import RetryGoalRuntimeRequest

        return await invoke(
            FieldCodec.encode(RetryGoalRuntimeRequest),
            goal_id=goal.id, expected_revision=goal.revision,
        )

    def change(self, ctx: GoalActionContext) -> Goal:
        if self.expect.expected_owner is None:
            raise ValueError("Retry requires the executing owner's private goal authority.")
        goal = ctx.thread.goal.require()
        if not isinstance(goal.state, BlockedState):
            raise ValueError("The blocked goal changed; refresh its state.")
        store = ctx.goals.open_goal_store()
        generation = store.require_generation(goal.id)
        generation.lifecycle.authorize_retry(store, generation, uuid4().hex)
        return replace(goal, state=ActiveGoal(), revision=goal.revision + 1)
