"""Goal commands own behavior; one parent owns CAS, publication and actor checks."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar, Annotated
from uuid import uuid4

from agent_comms import field_codec
from agent_comms.input_disposition import InputDispositions

from .change_report import FileLine, NoSourceReference
from .child_process import ProcessIdentity
from .command import Command
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
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
from agent_comms.goals import (
    Goal,
    GoalCheckpoint,
    GoalChangedError,
    GoalDeclaration,
    GoalOrder,
    NoGoal,
    ProblemGoalOrder,
    ReportedTurn,
    RequestDirective,
    RequestGoalOrder,
    UnorderedGoalOrder,
    UnobservedGoalCheckpoint,
)
from .progress_claims import ProgressClaim
from .recorded_time import EventTime
from .repository_name import RepositoryName
from .thread_identity import ThreadIncarnation
from .threads import Thread
from .todos import GoalRef, PendingProblemOrder, RejectingVerdict, Todo, TodoOperation

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
    expected_checkpoint: Annotated[
        GoalCheckpoint, field_codec.WireOmitDefault()
    ] = UnobservedGoalCheckpoint()

    def check(self, ctx: GoalActionContext) -> None:
        thread, goal = ctx.thread, ctx.thread.goal
        if not self.expected_checkpoint.matches(goal):
            raise GoalChangedError(thread, self.expected_checkpoint)
        if self.expected_owner is not None and (
            thread.process_identity != self.expected_owner
            or not ctx.goals.registry.status(thread.name).running
        ):
            raise ValueError("The goal owner changed; refresh its state.")
        self.expected_goal.check_expected(goal)
        if self.goal_id is not None:
            goal.require().require_id(self.goal_id)
        if self.expected_state is not None:
            goal.require().require_state(self.expected_state)


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
class GoalAction[Result: GoalDeclaration](DeclaredFamily, Command, affix="GoalAction"):
    expect: GoalPrecondition = GoalPrecondition()
    preserve_wait: ClassVar[bool] = False
    schedules_goal: ClassVar[bool] = False

    @classmethod
    def model_choices(cls) -> tuple[str, ...]:
        return tuple(member.declared_name for member in cls.members_with(ModelInvocable))

    @classmethod
    def model_precondition(cls, goal_id: str) -> GoalPrecondition:
        """What the unit's own goal tool expects before this action: its active goal."""
        return GoalPrecondition(goal_id=goal_id, expected_state=ActiveGoal())

    @classmethod
    async def submit_control(
        cls,
        observe: Callable[[], Awaitable[GoalSnapshot]],
        invoke: Callable[..., Awaitable[dict]],
        text: str = "",
    ) -> dict:
        goal = (await observe()).goal.require()
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

    def prepare(self, goals: Goals, name: str, actor: type) -> GoalAction[Result]:
        return self

    def apply(self, ctx: GoalActionContext) -> Result:
        self.expect.check(ctx)
        self.require_actor(ctx.actor)
        if ctx.model_report and any(
            entry.reports_turn(ctx.report_turn)
            for entry in ctx.goals.registry.goal_history(ctx.thread.name)
        ):
            raise ValueError("This goal was already reported in this turn.")
        goal = ctx.thread.goal.apply_action(self, ctx)
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
        if not self.preserve_wait:
            ctx.thread.goal.clear_wait(ctx.goals.waits)
        return goal

    @abstractmethod
    def change(self, goal: Goal, ctx: GoalActionContext) -> Result: ...

    def create(self, ctx: GoalActionContext) -> Result:
        raise RelationViolationError("No goal is set for this action.")

    def before_publish(self, goal: GoalDeclaration, ctx: GoalActionContext) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class TransitionGoalAction(GoalAction[Goal]):
    progress: str | None = None

    @abstractmethod
    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState: ...

    def change(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        goal.order.require_order(goal, ctx.goals, ctx.thread.incarnation)
        state = self.next_state(goal, ctx)
        self.require_transition(goal, state, ctx)
        self.before_transition(goal, ctx)
        changed = replace(
            goal,
            state=state,
            revision=goal.revision + 1,
            progress=goal.progress if self.progress is None else self.progress,
            reported_turn=ReportedTurn(ctx.report_turn) if ctx.model_report else goal.reported_turn,
        )
        self.before_goal_publish(changed, ctx)
        return changed

    def require_transition(self, goal: Goal, state: GoalState, ctx: GoalActionContext) -> None:
        """The goal's own state machine allows this transition."""
        goal.state.check_transition(state, owner=ctx.actor is OwnerInvocable)

    @classmethod
    def require_claims(cls, claims: tuple[ProgressClaim, ...]) -> None:
        """Only a completed report claims a state, so only it needs a claim attached."""

    def before_goal_publish(self, goal: Goal, ctx: GoalActionContext) -> None:
        pass

    def before_transition(self, goal: Goal, ctx: GoalActionContext) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class ActiveGoalAction(
    TransitionGoalAction, ModelInvocable, OwnerInvocable, RuntimeInvocable, OwnerControlInvocable
):
    schedules_goal = True

    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def before_transition(self, goal: Goal, ctx: GoalActionContext) -> None:
        thread = ctx.thread
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
        elif not generation.lifecycle.allows_resume(thread.turn_record.executing):
            raise ValueError("The goal attempt is unresolved; inspect it before Retry.")


@dataclass(frozen=True, kw_only=True)
class StandbyGoalAction(TransitionGoalAction, ModelInvocable, RuntimeInvocable):
    wait_for: tuple[str, ...] = ()
    reviewed_inputs: tuple[str, ...] = ()
    preserve_wait = True

    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def before_goal_publish(self, goal: Goal, ctx: GoalActionContext) -> None:
        thread = ctx.thread
        report_turn = ctx.report_turn
        review = ctx.goals._goal_input_review(thread, goal.id, self.wait_for)
        wait_targets = review.targets
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
                f"Call comms_inbox with standby={{'goal_id': {goal.id!r}, "
                f"'wait_for': {list(self.wait_for)!r}}}. Inspect standby_review.messages, then "
                "pass only standby_review.reviewed_inputs to comms_goal to wait "
                "for a later reply. Do not pass excluded owner or other dependency inputs. "
                "This does not mark them STARTED or replay them."
            )
        snapshot = ctx.goals.registry.snapshot()
        if not any(
            ctx.goals.agents.is_live(target.incarnation, snapshot)
            for target in wait_targets
        ):
            names = ", ".join(f"@{target.incarnation.name}" for target in wait_targets)
            raise ValueError(
                f"No declared dependency is live ({names}). "
                "Inspect its registered process before declaring standby."
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
                report_turn_id=reporting_lease.stored_turn_id(),
                report_turn_generation=reporting_lease.stored_generation(),
                target_turn_generations=tuple(
                    snapshot.threads[snapshot.canonical_name(target.incarnation.name)]
                    .turn_lease.stored_generation()
                    for target in wait_targets
                ),
            )
        )


@dataclass(frozen=True, kw_only=True)
class CompletionGoalAction(TransitionGoalAction):
    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return CompletedGoal()


@dataclass(frozen=True, kw_only=True)
class CompletedGoalAction(CompletionGoalAction, ModelInvocable, OwnerInvocable, RuntimeInvocable):
    def before_transition(self, goal: Goal, ctx: GoalActionContext) -> None:
        goal.order.require_completion(goal, ctx.goals, ctx.thread.incarnation)

    @classmethod
    def require_claims(cls, claims: tuple[ProgressClaim, ...]) -> None:
        ProgressClaim.require_claimed(claims, "completed")


@dataclass(frozen=True, kw_only=True)
class HeldOrderGoalAction(GoalAction[Goal], RuntimeInvocable):
    """A Todo operation on a held order and the revision of the holder's Goal, in one write."""
    problem: str
    operation: TodoOperation

    @abstractmethod
    def revised(self, goal: Goal, ctx: GoalActionContext) -> Goal: ...

    def prepare(self, goals, name, actor):
        ctx = GoalActionContext(goals, goals.registry.require(name), actor)
        self.expect.check(ctx)
        goal = ctx.thread.goal.require()
        todo = goals.todos.get(self.problem)
        todo.require_assignee(ctx.thread.incarnation, goal.id)
        operation = self.operation.prepare(goals, todo)
        return replace(self, operation=operation, expect=replace(self.expect, expected_goal=goal))

    def change(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        ctx.goals.todos.apply(self.problem, self.operation)
        return replace(self.revised(goal, ctx), revision=goal.revision + 1)


@dataclass(frozen=True, kw_only=True)
class SentBackOrderGoalAction(HeldOrderGoalAction):
    operation: RejectingVerdict

    def revised(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        return goal


@dataclass(frozen=True, kw_only=True)
class BlockedGoalAction(TransitionGoalAction, ModelInvocable, OwnerInvocable, RuntimeInvocable):
    block_reason: str | None = None

    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        if ctx.model_report:
            goal.order.require_model_block()
        return BlockedGoal(
            required_block_reason(
                self.block_reason if self.block_reason is not None else self.progress
            )
        )


@dataclass(frozen=True, kw_only=True)
class PausedGoalAction(
    TransitionGoalAction, OwnerInvocable, RuntimeInvocable, OwnerControlInvocable
):
    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        current = goal.state.pause_source
        return PausedGoal(
            current or (OwnerPause() if ctx.actor is OwnerInvocable else RuntimePause())
        )


@dataclass(frozen=True, kw_only=True)
class ReplacementGoalAction[Result: GoalDeclaration](GoalAction[Result]):
    def before_publish(self, goal: GoalDeclaration, ctx: GoalActionContext) -> None:
        for previous in ctx.thread.goal:
            self.retire(previous, ctx)

    @staticmethod
    def retire(goal: Goal, ctx: GoalActionContext) -> None:
        # Revoke the old protected goal before replacing its registry identity.
        attempts = ctx.goals.open_goal_store()
        generation = attempts.snapshot(goal.id)
        if generation is not None and not generation.lifecycle.terminal:
            attempts.retire_goal(
                goal.id, expected_generation=generation.number,
                attempt_id=generation.attempt_id,
            )


@dataclass(frozen=True, kw_only=True)
class SetGoalAction(ReplacementGoalAction[Goal], OwnerInvocable, RuntimeInvocable):
    text: str
    order: GoalOrder = field(default_factory=UnorderedGoalOrder)
    goal_id: str = field(default_factory=lambda: uuid4().hex)

    @classmethod
    async def submit_control(cls, observe, invoke, text: str = "") -> dict:
        from .field_codec import FieldCodec
        from .runtime_requests import SetGoalRuntimeRequest

        return await invoke(FieldCodec.encode(SetGoalRuntimeRequest), text=text)

    def change(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        if goal.id == self.goal_id:
            if (goal.text, goal.order) != (self.text.strip(), self.order):
                raise ValueError("Goal identity already has different instructions.")
            return goal
        goal.order.require_replacement(goal, ctx.goals, ctx.thread.incarnation)
        return self.create(ctx)

    def create(self, ctx: GoalActionContext) -> Goal:
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
class DirectiveGoalAction(GoalAction[GoalDeclaration], RuntimeInvocable):
    """A claude-ai message to a thread that holds owner requests becomes one of its requests."""

    text: str
    seq: int
    at: float

    def change(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        return goal.order.directed(goal, self, ctx)

    def create(self, ctx: GoalActionContext) -> GoalDeclaration:
        return ctx.thread.goal

    def directive(self) -> RequestDirective:
        return RequestDirective(self.seq, self.at + RequestDirective.window)

    def request(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        ReplacementGoalAction.retire(goal, ctx)
        order = RequestGoalOrder((self.directive(),))
        return SetGoalAction(text=self.text, order=order).change(goal, ctx)


@dataclass(frozen=True, kw_only=True)
class ProblemOrderGoalAction(GoalAction[Goal], RuntimeInvocable):
    problem: str
    order: PendingProblemOrder
    request: str
    review_at: EventTime
    commander: str

    @abstractmethod
    def bound_to(self, actor: ThreadIncarnation) -> ProblemOrderGoalAction: ...

    @abstractmethod
    def reserve(self, ctx: GoalActionContext, generation: str, goal: GoalRef) -> Todo: ...

    def change(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        return goal.state.accept_order(self, goal, ctx)

    def continue_goal(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        request = self.request_ref(ctx)
        self.reserve(ctx, goal.id, request)
        revision = goal.revision + 1
        return replace(goal, order=ProblemGoalOrder(), text=ProblemGoalOrder.text, progress="",
                       revision=revision, mention_source=bind_goal_mentions(
                           ProblemGoalOrder.text, goal.id, revision, ctx.thread,
                           ctx.goals.registry.snapshot()))

    def request_ref(self, ctx: GoalActionContext) -> GoalRef:
        """Refuse invalid orders before reserving their Todo."""
        self.require_sendable()
        commander = ctx.goals.registry.require(self.commander)
        commander.goal.require().require_id(self.request).require_active().order.require_request()
        self.review_at.require_recorded("A review time")
        request = GoalRef(commander.name, commander.created_at, self.request)
        return request

    def require_sendable(self) -> None:
        """A new order's wording and source references hold at its declared revision."""
        self.order.require_wording()
        self.order.require_references()
        self.order.check.require_directory()

    def create(self, ctx: GoalActionContext) -> Goal:
        request = self.request_ref(ctx)
        fresh = SetGoalAction(text=ProblemGoalOrder.text, order=ProblemGoalOrder())
        self.reserve(ctx, fresh.goal_id, request)
        return fresh.create(ctx)


@dataclass(frozen=True, kw_only=True)
class OrderGoalAction(ProblemOrderGoalAction):
    """The order's commander gives the problem to this thread."""

    expected_revision: int
    traced: Annotated[FileLine | NoSourceReference, field_codec.WireOmitDefault()] = (
        NoSourceReference()
    )
    assigner: ThreadIncarnation

    def bound_to(self, actor: ThreadIncarnation) -> OrderGoalAction:
        return replace(self, commander=actor.name, assigner=actor)

    def reserve(self, ctx: GoalActionContext, generation: str, goal: GoalRef) -> Todo:
        return ctx.goals.todos.assign(self.problem, expected_revision=self.expected_revision,
            owner=ctx.thread, assigner=self.assigner,
            generation=generation, goal=goal, review_at=self.review_at, order=self.order,
            traced=self.traced)


@dataclass(frozen=True, kw_only=True)
class ReissuedOrderGoalAction(OrderGoalAction):
    """Core hands a holder an order its Todo already accepted.

    The order was sendable when accepted; its unit has edited the sources it names
    since, so they are not checked again against the moved tree.
    """

    def require_sendable(self) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class CreateProblemGoalAction(ProblemOrderGoalAction):
    repo: RepositoryName
    text: str

    def bound_to(self, actor: ThreadIncarnation) -> CreateProblemGoalAction:
        return replace(self, commander=actor.name)

    def reserve(self, ctx: GoalActionContext, generation: str, goal: GoalRef) -> Todo:
        return ctx.goals.todos.create(self.problem, self.repo, self.text,
            creator=ctx.goals.registry.require(self.commander), owner=ctx.thread,
            generation=generation, goal=goal, review_at=self.review_at, order=self.order)


@dataclass(frozen=True, kw_only=True)
class ClearGoalAction(
    ReplacementGoalAction[NoGoal], OwnerInvocable, RuntimeInvocable, OwnerControlInvocable
):
    def change(self, goal: Goal, ctx: GoalActionContext) -> NoGoal:
        goal.order.require_replacement(goal, ctx.goals, ctx.thread.incarnation)
        return self.create(ctx)

    def create(self, ctx: GoalActionContext) -> NoGoal:
        return NoGoal()


@dataclass(frozen=True, kw_only=True)
class EditGoalAction(GoalAction[Goal], OwnerInvocable, RuntimeInvocable):
    text: str
    preserve_wait = True

    def change(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        goal.order.require_replacement(goal, ctx.goals, ctx.thread.incarnation)
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
class RetryGoalAction(TransitionGoalAction, ModelInvocable, OwnerInvocable, RuntimeInvocable):
    """Retry a blocked goal: the unit itself, its commander or the owner; authorize_retry fences
    every attempt against the unresolved one."""

    @classmethod
    def model_precondition(cls, goal_id: str) -> GoalPrecondition:
        return GoalPrecondition(goal_id=goal_id)

    @classmethod
    async def submit_checkpoint(cls, invoke, goal: Goal) -> dict:
        from .field_codec import FieldCodec
        from .runtime_requests import RetryGoalRuntimeRequest

        return await invoke(
            FieldCodec.encode(RetryGoalRuntimeRequest),
            goal_id=goal.id, expected_revision=goal.revision,
        )

    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def require_transition(self, goal: Goal, state: GoalState, ctx: GoalActionContext) -> None:
        """A blocked goal leaves its state machine only through the attempt ledger's retry."""
        if not isinstance(goal.state, BlockedState):
            raise ValueError("The blocked goal changed; refresh its state.")
        store = ctx.goals.open_goal_store()
        store.authorize_current_retry(goal.id, uuid4().hex)
