"""Goal commands own behavior; one parent owns CAS, publication and actor checks."""


import time
from abc import abstractmethod
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from pydantic import JsonValue

from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.input_key import InputKey, InputKeyText
from agent_comms.execution.reported_turn import ReportedTurnState
from agent_comms.nominal import field_codec
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    FieldCodec,
    GoalId,
    GoalWaitId,
    MessageId,
    RetryDecisionId,
)
from agent_comms.nominal.lifecycle_capability import Settled
from agent_comms.planning.attempt_resolution import ComplianceDecision, OrderDecision, RetryDecision
from agent_comms.planning.command import Command
from agent_comms.planning.goal_control import GoalControlSource
from agent_comms.planning.goal_mentions import bind_goal_mentions
from agent_comms.planning.goal_mutation import GoalMutation, GoalStateOrder
from agent_comms.planning.goal_records import GoalRecords
from agent_comms.planning.goal_scope import AllGoals, OneGoal
from agent_comms.planning.goal_states import (
    ActiveGoal,
    BlockedGoal,
    ChokeholdPause,
    CompletedGoal,
    GoalState,
    ModelPause,
    OwnerPause,
    PausedGoal,
    PauseSource,
    RuntimePause,
)
from agent_comms.planning.goal_wait_recovery import GoalInputReview
from agent_comms.planning.goal_waits import GoalWaits, RecordedGoalWait
from agent_comms.planning.goals import (
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
from agent_comms.planning.orders import (
    PendingProblemOrder,
    RecordOrderHeadVerdict,
    SendBackVerdict,
    Todo,
    TodoCommand,
)
from agent_comms.planning.owner_requests import OwnerRequestId
from agent_comms.planning.problem_findings import ExpectedProblem
from agent_comms.planning.progress_claims import ProgressClaim
from agent_comms.projects.change_report import CitationProject, FileLine, NoSourceReference
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.registry.registry_document import TeamProject
from agent_comms.registry.registry_store import RegistryEdit
from agent_comms.registry.thread_identity import CoreWatcherIncarnation, ThreadIncarnation
from agent_comms.registry.threads import Thread
from agent_comms.storage.recorded_time import EventTime
from agent_comms.transport.owner_connection import CapturedOwner
from agent_comms.transport.runtime_request import AttachedRuntimeRequest


class GoalActor:
    """Who invokes a goal action. An action declares the actors it admits by inheriting them;
    each actor owns whether it is the goal's owner, whether it reports a turn, what a pause it
    takes records, and whose consent its change needs."""

    actor_is_owner: ClassVar[bool] = False
    actor_reports_turn: ClassVar[bool] = False

    @classmethod
    def require_owner_control(cls) -> None:
        raise ValueError("Goal updates support only active, paused, or clear.")

    @classmethod
    def actor_pause_source(cls) -> PauseSource:
        return RuntimePause()

    @classmethod
    def require_actor_consent(cls, goal: GoalDeclaration) -> None:
        """The thread itself, its owner and its runtime need no one else's consent."""


class ModelInvocable(GoalActor):
    actor_reports_turn = True


class OwnerInvocable(GoalActor):
    actor_is_owner = True

    @classmethod
    def actor_pause_source(cls) -> PauseSource:
        return OwnerPause()


class RuntimeInvocable(GoalActor):
    pass


class OwnerControlInvocable(GoalActor):

    @classmethod
    def require_owner_control(cls) -> None:
        pass


class PeerInvocable(GoalActor):
    """A teammate acting on another thread's goal. Responsibility is shared and there is no
    rank, so a teammate may set, edit, pause, resume or clear it as the thread itself could,
    under the same order rules (held problems still refuse replacement). What it may not do is
    change a goal under a protected pause: Tristan's pause and the chokehold lift only through
    their own owners."""

    @classmethod
    def actor_pause_source(cls) -> PauseSource:
        return ModelPause()

    @classmethod
    def require_actor_consent(cls, goal: GoalDeclaration) -> None:
        for current in goal:
            current.state.require_peer_change()


@dataclass(frozen=True)
class GoalPrecondition:
    goal_id: OneGoal | AllGoals = AllGoals()
    expected_state: GoalState | None = None
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
        self.goal_id.require(goal)
        if self.expected_state is not None:
            goal.require().require_state(self.expected_state)


@dataclass(frozen=True)
class GoalActionContext:
    goals: GoalRecords
    thread: Thread
    actor: type = RuntimeInvocable

    @property
    def reported_turn(self) -> ReportedTurnState:
        return self.thread.turn_record.read().reported_turn

    @property
    def model_report(self) -> bool:
        return self.actor.actor_reports_turn


@dataclass(frozen=True, kw_only=True)
class GoalPublicationContext(GoalActionContext):
    """An action may publish only through the registry edit that checked its original."""

    edit: RegistryEdit


@dataclass(frozen=True, kw_only=True)
class GoalAction[Result: GoalDeclaration](
    DeclaredFamily, Command[GoalPublicationContext, Result],
    GoalMutation[Goal, GoalActionContext, Result], affix="GoalAction"
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
    async def submit_control[Authority](
        cls, source: GoalControlSource[Authority, GoalAction[GoalDeclaration], Goal, GoalDeclaration],
        authority: Authority, text: str = "",
    ) -> GoalDeclaration:
        goal = (await source.observed_goal(authority)).require()
        return await cls.submit_checkpoint(source, authority, goal)

    @classmethod
    async def submit_checkpoint[Authority](
        cls, source: GoalControlSource[Authority, GoalAction[GoalDeclaration], Goal, GoalDeclaration],
        authority: Authority, goal: Goal,
    ) -> GoalDeclaration:
        return await source.change_goal_control(authority, cls, goal)

    def require_actor(self, actor: type) -> None:
        if not isinstance(self, actor):
            raise ValueError(f"This actor cannot take goal action {self.declared_name!r}.")

    def prepare(self, goals: GoalRecords, name: str, actor: type) -> GoalAction[Result]:
        return self

    def apply(self, ctx: GoalPublicationContext) -> Result:
        self.expect.check(ctx)
        self.require_actor(ctx.actor)
        ctx.actor.require_actor_consent(ctx.thread.goal)
        goal = ctx.thread.goal.apply_action(self, ctx)
        if goal == ctx.thread.goal:
            return goal
        self.before_publish(goal, ctx)
        change = ctx.edit.document.prepare_registration(
            replace(ctx.thread, goal=goal),
            ctx.edit.document.status(ctx.thread.name), new_owner=False,
        )
        ctx.goals.registry._commit_registration(ctx.edit, change)
        if not self.preserve_wait:
            ctx.thread.goal.clear_wait(ctx.goals.waits.clear)
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
        )
        self.before_goal_publish(changed, ctx)
        return changed

    def require_transition(self, goal: Goal, state: GoalState, ctx: GoalActionContext) -> None:
        """The goal's own state machine allows this transition."""
        goal.state.check_transition(state, owner=ctx.actor.actor_is_owner)

    @classmethod
    def require_claims(cls, claims: tuple[ProgressClaim, ...]) -> None:
        """Only a completed report claims a state, so only it needs a claim attached."""

    def before_goal_publish(self, goal: Goal, ctx: GoalActionContext) -> None:
        pass

    def before_transition(self, goal: Goal, ctx: GoalActionContext) -> None:
        pass


class GoalReportAction(TransitionGoalAction):
    """Keep the original reporting turn while progress and decisions evolve.

    Native terminal settlement belongs to TurnGoalAccount and the attempt ledger,
    not to how many times this turn updates its goal.
    """

    def change(self, goal: Goal, ctx: GoalActionContext) -> Goal:
        if ctx.model_report:
            goal = replace(goal, reported_turn=ctx.reported_turn)
        return super().change(goal, ctx)


@dataclass(frozen=True, kw_only=True)
class ActiveGoalAction(
    GoalReportAction, ModelInvocable, OwnerInvocable, RuntimeInvocable, OwnerControlInvocable,
    PeerInvocable,
):
    schedules_goal = True

    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def before_transition(self, goal: Goal, ctx: GoalActionContext) -> None:
        generation = ctx.goals.open_goal_store().require_generation(goal.id)
        generation.lifecycle.resume_status(ctx.thread.executing)


@dataclass(frozen=True, kw_only=True)
class CompliedGoalAction(TransitionGoalAction, OwnerInvocable):
    """The teammate complied, so a unit its chokehold paused works again. The compliant answer
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
        resumed.lifecycle.resume_status(ctx.thread.executing)


@dataclass(frozen=True, kw_only=True)
class StandbyGoalAction(GoalReportAction, ModelInvocable, RuntimeInvocable):
    wait_for: tuple[str, ...] = ()
    reviewed_inputs: tuple[Annotated[InputKey, InputKeyText], ...] = ()
    preserve_wait = True

    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def prepare(self, goals: GoalRecords, name: str, actor: type) -> GoalAction[Goal]:
        ctx = GoalActionContext(goals, goals.registry.require(name), actor)
        self.expect.check(ctx)
        review = goals._goal_input_review(ctx.thread, ctx.thread.goal.require().id, self.wait_for)
        self.expect.check(GoalActionContext(goals, review.owner, actor))
        return PreparedStandbyGoalAction(
            expect=replace(self.expect, expected_checkpoint=review.owner.goal.captured),
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
        dispositions = ctx.goals.dispositions
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
            ctx.goals.agents.answers_later(target.incarnation, snapshot)
            for target in wait_targets
        ):
            names = ", ".join(f"@{target.incarnation.name}" for target in wait_targets)
            raise ValueError(
                f"No thread this wait names will answer ({names}): none has a live bridge or "
                "process. Inspect its registered process before declaring standby."
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
class CompletionGoalAction(GoalReportAction):
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
class BlockedGoalAction(GoalReportAction, ModelInvocable, OwnerInvocable, RuntimeInvocable):
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
    TransitionGoalAction, OwnerInvocable, RuntimeInvocable, OwnerControlInvocable, PeerInvocable
):
    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        current = goal.state.pause_source
        return PausedGoal(
            current or ctx.actor.actor_pause_source()
        )


@dataclass(frozen=True, kw_only=True)
class ChokeholdPausedGoalAction(PausedGoalAction):
    """The heartbeat holds a unit while its teammate ignores a vibe check."""

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
class SetGoalAction(ReplacementGoalAction[Goal], OwnerInvocable, RuntimeInvocable, PeerInvocable):
    text: str
    order: GoalOrder = field(default_factory=UnorderedGoalOrder)
    goal_id: GoalId = field(default_factory=GoalId.mint)

    @classmethod
    async def submit_control[Authority](
        cls, source: GoalControlSource[Authority, GoalAction[GoalDeclaration], Goal, GoalDeclaration],
        authority: Authority, text: str = "",
    ) -> GoalDeclaration:
        return await source.set_goal_control(authority, text)

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
class ProblemOrderGoalAction(
    GoalAction[Goal], RuntimeInvocable, GoalStateOrder[Goal, GoalActionContext],
):
    problem: str
    order: PendingProblemOrder
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
        revision = goal.revision + 1
        return replace(goal, order=ProblemGoalOrder(), text=ProblemGoalOrder.text, progress="",
                       revision=revision, mention_source=bind_goal_mentions(
                           ProblemGoalOrder.text, goal.id, revision, ctx.thread,
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
        fresh = SetGoalAction(text=ProblemGoalOrder.text, order=ProblemGoalOrder())
        self.reserve(ctx, fresh.goal_id)
        return fresh.create(ctx)


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
class CreateProblemGoalAction(ProblemOrderGoalAction):
    repo: RepositoryName
    text: str

    def reserve(self, ctx: GoalActionContext, generation: GoalId) -> None:
        ctx.goals.todos.create(self.problem, self.repo, self.text,
            creator=self.sender_teammate(ctx), owner=ctx.thread,
            assigner=self.assigner, generation=generation, request=self.request,
            review_at=self.review_at, order=self.order)


@dataclass(frozen=True, kw_only=True)
class ClearGoalAction(
    ReplacementGoalAction[NoGoal], OwnerInvocable, RuntimeInvocable, OwnerControlInvocable,
    PeerInvocable,
):
    def change(self, goal: Goal, ctx: GoalActionContext) -> NoGoal:
        goal.order.require_replacement(goal, ctx.goals, ctx.thread.incarnation)
        return self.create(ctx)

    def create(self, ctx: GoalActionContext) -> NoGoal:
        return NoGoal()


@dataclass(frozen=True, kw_only=True)
class EditGoalAction(GoalAction[Goal], OwnerInvocable, RuntimeInvocable, PeerInvocable):
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
    """Retry a blocked goal: the unit itself, its teammate or the owner; authorize_retry fences
    every attempt against the unresolved one."""

    @classmethod
    def model_precondition(cls, goal_id: GoalId) -> GoalPrecondition:
        return GoalPrecondition(goal_id=OneGoal(goal_id))

    @classmethod
    async def submit_checkpoint[Authority](
        cls, source: GoalControlSource[Authority, GoalAction[GoalDeclaration], Goal, GoalDeclaration],
        authority: Authority, goal: Goal,
    ) -> GoalDeclaration:
        return await source.retry_goal_control(authority, goal)

    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def require_transition(self, goal: Goal, state: GoalState, ctx: GoalActionContext) -> None:
        """A blocked goal leaves its state machine only through the attempt ledger's retry."""
        store = ctx.goals.open_goal_store()
        store.authorize_current_retry(goal.id, RetryDecision(RetryDecisionId.mint()))
