"""Goal commands own behavior; one parent owns CAS, publication and actor checks."""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from agent_comms.field_codec import GoalId, GoalWaitId, MessageId, RetryDecisionId
from agent_comms import field_codec
from agent_comms.goals import (
    Goal,
    GoalChangedError,
    GoalCheckpoint,
    GoalDeclaration,
    GoalOrder,
    NoGoal,
    ProblemGoalOrder,
    UnobservedGoalCheckpoint,
    UnorderedGoalOrder,
)

from .field_codec import FieldCodec
from .input_key import InputKey, InputKeyText
from agent_comms.input_disposition import InputDispositions
from agent_comms.todos import (
    PendingProblemOrder, RecordOrderHeadVerdict, SendBackVerdict, Todo, TodoCommand, TodoOperation,
)

from .change_report import FileLine, NoSourceReference
from .child_process import ProcessIdentity
from .command import Command
from .declared_family import DeclaredFamily
from .errors import OverdueHeadRefusalError, RelationViolationError
from .goal_mentions import bind_goal_mentions
from .goal_scope import AllGoals, OneGoal
from .goal_states import (
    ActiveGoal,
    BlockedGoal,
    ChokeholdPause,
    CompletedGoal,
    GoalState,
    OwnerPause,
    PausedGoal,
    RuntimePause,
)
from .goal_waits import GoalInputReview, GoalWaits, RecordedGoalWait
from .lifecycle_capability import Settled
from .owner_requests import OwnerRequestId
from .progress_claims import ProgressClaim
from .recorded_time import EventTime
from .repository_name import RepositoryName
from .standing_rules import OrderRefusingStandingRule
from .thread_identity import ThreadIncarnation
from .threads import Thread
from .reported_turn import ReportedTurnState
from .attempt_resolution import ComplianceDecision, OrderDecision, RetryDecision

if TYPE_CHECKING:
    from .goal_management import Goals
    from .registry_document import CommanderProject
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
    goal_id: OneGoal | AllGoals = AllGoals()
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
        self.goal_id.require(goal)
        if self.expected_state is not None:
            goal.require().require_state(self.expected_state)


@dataclass(frozen=True)
class GoalActionContext:
    goals: Goals
    thread: Thread
    actor: type = RuntimeInvocable

    @property
    def reported_turn(self) -> ReportedTurnState:
        return self.thread.turn_record.read().reported_turn

    @property
    def model_report(self) -> bool:
        return self.actor is ModelInvocable


@dataclass(frozen=True, kw_only=True)
class GoalAction[Result: GoalDeclaration](
    DeclaredFamily, Command[GoalActionContext, Result], affix="GoalAction"
):
    expect: GoalPrecondition = GoalPrecondition()
    preserve_wait: ClassVar[bool] = False
    schedules_goal: ClassVar[bool] = False

    @classmethod
    def model_choices(cls) -> tuple[str, ...]:
        return tuple(member.declared_name for member in cls.members_with(ModelInvocable))

    @classmethod
    def model_precondition(cls, goal_id: GoalId) -> GoalPrecondition:
        """What the unit's own goal tool expects before this action: its active goal."""
        return GoalPrecondition(goal_id=OneGoal(goal_id), expected_state=ActiveGoal())

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
            entry.reports_turn(ctx.reported_turn)
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

    def require_order(self, goal: Goal, ctx: GoalActionContext) -> None:
        """A transition works under the goal's order, so the order must still hold."""
        goal.order.require_order(goal, ctx.goals, ctx.thread.incarnation)

    def change(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        self.require_order(goal, ctx)
        state = self.next_state(goal, ctx)
        self.require_transition(goal, state, ctx)
        self.before_transition(goal, ctx)
        changed = replace(
            goal,
            state=state,
            revision=goal.revision + 1,
            progress=goal.progress if self.progress is None else self.progress,
            reported_turn=ctx.reported_turn if ctx.model_report else goal.reported_turn,
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
        generation = ctx.goals.open_goal_store().require_generation(goal.id)
        generation.lifecycle.resume_status(generation, goal, ctx)


@dataclass(frozen=True, kw_only=True)
class CompliedGoalAction(TransitionGoalAction, OwnerInvocable):
    """The commander complied, so a unit its chokehold paused works again. The compliant answer
    is a witness the way an order is: a generation an interruption left unresolved continues
    as a fresh order continues it (continue_work), where a status-only resume is refused.
    Only Core's release takes it; no model or control surface can name a witness."""

    witness: MessageId

    def require_order(self, goal: Goal, ctx: GoalActionContext) -> None:
        """Lifting the heartbeat's hold restores the goal it paused. Its problems may close
        during the hold, and that must not keep this unit, or the units after it, held."""

    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def before_transition(self, goal: Goal, ctx: GoalActionContext) -> None:
        store = ctx.goals.open_goal_store()
        generation = store.require_generation(goal.id)
        generation.lifecycle.continue_work(store, generation, ComplianceDecision(self.witness))
        resumed = store.require_generation(goal.id)
        resumed.lifecycle.resume_status(resumed, goal, ctx)


@dataclass(frozen=True, kw_only=True)
class StandbyGoalAction(TransitionGoalAction, ModelInvocable, RuntimeInvocable):
    wait_for: tuple[str, ...] = ()
    reviewed_inputs: tuple[Annotated[InputKey, InputKeyText], ...] = ()
    preserve_wait = True

    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def prepare(self, goals: Goals, name: str, actor: type) -> GoalAction[Goal]:
        ctx = GoalActionContext(goals, goals.registry.require(name), actor)
        self.expect.check(ctx)
        review = goals._goal_input_review(ctx.thread, ctx.thread.goal.require().id, self.wait_for)
        self.expect.check(GoalActionContext(goals, review.owner, actor))
        return PreparedStandbyGoalAction(
            expect=replace(self.expect, expected_goal=review.owner.goal),
            progress=self.progress,
            wait_for=self.wait_for,
            reviewed_inputs=self.reviewed_inputs,
            review=review,
        )


@dataclass(frozen=True, kw_only=True)
class PreparedStandbyGoalAction(StandbyGoalAction, declared_name=None):
    """Only the action prepared from real replies can publish this exact wait."""

    review: GoalInputReview

    def before_goal_publish(self, goal: Goal, ctx: GoalActionContext) -> None:
        review = self.review
        review.require_current(ctx.goals, ctx.thread)
        thread = ctx.thread
        reported_turn = ctx.reported_turn
        wait_targets = review.targets
        aliases = review.owners
        dispositions = InputDispositions(ctx.goals.root)
        unknown = {row.key: row for row in review.unknown}
        # An input delivered into this recipient's turn after the inbox cut is already handled.
        delivered = dispositions.read(
            tuple(key for key in self.reviewed_inputs if key not in unknown)).started_for(aliases)
        reviewed_keys = tuple(key for key in dict.fromkeys(self.reviewed_inputs)
                              if key not in delivered)
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
            turn_id=reported_turn,
        )
        # Commit scheduling authority first. A crash before the registry
        # progress update must leave this same goal waiting, not runnable.
        reporting_lease = thread.turn_lease
        ctx.goals.waits.record(
            RecordedGoalWait(
                goal.id,
                GoalWaitId.mint(),
                goal.revision,
                review.through_seq,
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
        return replace(self, operation=operation, expect=replace(self.expect, expected_goal=goal))

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
class ChokeholdPausedGoalAction(PausedGoalAction):
    """The heartbeat holds a unit while its commander ignores a vibe check."""

    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return PausedGoal(ChokeholdPause())


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
        if generation is not None and not generation.lifecycle.items_with(Settled, generation):
            attempts.retire_goal(
                goal.id, expected_generation=generation.number,
                attempt_id=generation.attempt_id,
            )


@dataclass(frozen=True, kw_only=True)
class SetGoalAction(ReplacementGoalAction[Goal], OwnerInvocable, RuntimeInvocable):
    text: str
    order: GoalOrder = field(default_factory=UnorderedGoalOrder)
    goal_id: GoalId = field(default_factory=GoalId.mint)

    @classmethod
    async def submit_control(cls, observe, invoke, text: str = "") -> dict:
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
            published_seq=ctx.goals.bus.bus.revision(),
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
class ProblemOrderGoalAction(GoalAction[Goal], RuntimeInvocable):
    problem: str
    order: PendingProblemOrder
    request: OwnerRequestId
    review_at: EventTime
    commander: str
    assigner: ThreadIncarnation

    def bound_to(
        self, actor: ThreadIncarnation, commander: ThreadIncarnation,
    ) -> ProblemOrderGoalAction:
        return replace(self, commander=commander.name, assigner=actor)

    @abstractmethod
    def reserve(self, ctx: GoalActionContext, generation: GoalId) -> Todo: ...

    def change(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        return goal.state.accept_order(self, goal, ctx)

    def continue_goal(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        self.require_ready(ctx)
        self.reserve(ctx, goal.id)
        store = ctx.goals.open_goal_store()
        generation = store.require_generation(goal.id)
        generation.lifecycle.continue_work(store, generation, OrderDecision(self.order.id))
        revision = goal.revision + 1
        return replace(goal, order=ProblemGoalOrder(), text=ProblemGoalOrder.text, progress="",
                       revision=revision, mention_source=bind_goal_mentions(
                           ProblemGoalOrder.text, goal.id, revision, ctx.thread,
                           ctx.goals.registry.snapshot()))

    def prepare(self, goals: Goals, name: str, actor: type) -> ProblemOrderGoalAction:
        self.require_reviewed(goals)
        return super().prepare(goals, name, actor)

    def require_reviewed(self, goals: Goals) -> None:
        """Read remote review facts before the registry lock or child allocation."""
        if refused := OrderRefusingStandingRule.refusals(goals, self.commander):
            raise OverdueHeadRefusalError("Review overdue heads before sending new orders:\n" + "\n".join(refused))

    def require_ready(self, ctx: GoalActionContext) -> None:
        """Recheck local request facts before goal reservation or allocating a child."""
        self.require_sendable(ctx.goals.registry.snapshot().commander_project)
        commander = ctx.goals.registry.require(self.commander)
        ctx.goals.todos.owner_request(self.request).require_open(commander.incarnation)
        self.review_at.require_recorded("A review time")

    def require_sendable(self, project: CommanderProject) -> None:
        """A new order's wording and source references hold at its declared revision."""
        self.order.require_wording()
        self.order.require_references(project)
        self.order.check.require_directory()

    def create(self, ctx: GoalActionContext) -> Goal:
        self.require_ready(ctx)
        fresh = SetGoalAction(text=ProblemGoalOrder.text, order=ProblemGoalOrder())
        self.reserve(ctx, fresh.goal_id)
        return fresh.create(ctx)


@dataclass(frozen=True, kw_only=True)
class OrderGoalAction(ProblemOrderGoalAction):
    """The order's commander gives the problem to this thread."""

    expected_revision: int
    traced: Annotated[FileLine | NoSourceReference, field_codec.WireOmitDefault()] = (
        NoSourceReference()
    )
    def reserve(self, ctx: GoalActionContext, generation: GoalId) -> Todo:
        return ctx.goals.todos.assign(self.problem, expected_revision=self.expected_revision,
            owner=ctx.thread, assigner=self.assigner,
            generation=generation, request=self.request, review_at=self.review_at, order=self.order,
            traced=self.traced)


@dataclass(frozen=True, kw_only=True)
class ReissuedOrderGoalAction(OrderGoalAction):
    """Core hands a holder an order its Todo already accepted.

    The order was sendable when accepted; its unit has edited the sources it names
    since, so they are not checked again against the moved tree.
    """

    @classmethod
    def for_todo(cls, todo: Todo, commander: str) -> Self:
        """The order the Todo already accepted, handed again with a fresh review window, by the
        actor that assigned it."""
        now = time.time()
        order = todo.order.reissued(now)
        return cls(
            problem=todo.id, expected_revision=todo.revision,
            order=order, request=todo.request.require(),
            review_at=todo.reissued_review(now), commander=commander,
            assigner=todo.acting_assigner,
            traced=order.owner)

    def require_sendable(self, project: CommanderProject) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class CreateProblemGoalAction(ProblemOrderGoalAction):
    repo: RepositoryName
    text: str

    def reserve(self, ctx: GoalActionContext, generation: GoalId) -> Todo:
        return ctx.goals.todos.create(self.problem, self.repo, self.text,
            creator=ctx.goals.registry.require(self.commander), owner=ctx.thread,
            assigner=self.assigner, generation=generation, request=self.request,
            review_at=self.review_at, order=self.order)


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
        goal.order.require_edit()
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
    def model_precondition(cls, goal_id: GoalId) -> GoalPrecondition:
        return GoalPrecondition(goal_id=OneGoal(goal_id))

    @classmethod
    async def submit_checkpoint(cls, invoke, goal: Goal) -> dict:
        from .runtime_requests import RetryGoalRuntimeRequest

        return await invoke(
            FieldCodec.encode(RetryGoalRuntimeRequest),
            goal_id=goal.id, expected_revision=goal.revision,
        )

    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def require_transition(self, goal: Goal, state: GoalState, ctx: GoalActionContext) -> None:
        """A blocked goal leaves its state machine only through the attempt ledger's retry."""
        store = ctx.goals.open_goal_store()
        store.authorize_current_retry(goal.id, RetryDecision(RetryDecisionId.mint()))
