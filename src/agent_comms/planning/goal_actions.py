"""Goal commands own behavior; one parent owns CAS, publication and actor checks."""


from abc import abstractmethod
from dataclasses import dataclass, field, replace
from typing import Annotated

from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.input_key import InputKey, InputKeyText
from agent_comms.execution.reported_turn import ReportedTurnState
from agent_comms.nominal import field_codec
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    GoalId,
    MessageId,
    RetryDecisionId,
)
from agent_comms.planning.attempt_resolution import ComplianceDecision, RetryDecision
from agent_comms.planning.command import Command
from agent_comms.planning.goal_action_store import GoalActionStore, PreparedStandbyReview
from agent_comms.planning.goal_control import GoalControlSource
from agent_comms.planning.goal_mentions import bind_goal_mentions
from agent_comms.planning.goal_mutation import GoalMutation
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
from agent_comms.planning.goals import (
    Goal,
    GoalChangedError,
    GoalCheckpoint,
    GoalDeclaration,
    GoalOrder,
    NoGoal,
    UnobservedGoalCheckpoint,
    UnorderedGoalOrder,
)
from agent_comms.planning.progress_claims import ProgressClaim
from agent_comms.registry.registry_store import RegistryEdit
from agent_comms.registry.threads import Thread


class GoalActor:
    """Who invokes a goal action. An action declares the actors it admits by inheriting them;
    each actor owns whether it is the goal's owner, whether it reports a turn, what a pause it
    takes records, and whose consent its change needs."""

    @classmethod
    def actor_is_owner(cls) -> bool:
        return False

    @classmethod
    def actor_reports_turn(cls) -> bool:
        return False

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

    @classmethod
    def actor_reports_turn(cls) -> bool:
        return True


class OwnerInvocable(GoalActor):

    @classmethod
    def actor_is_owner(cls) -> bool:
        return True

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
            or not ctx.goals.is_thread_running(thread.name)
        ):
            raise ValueError("The goal owner changed; refresh its state.")
        self.goal_id.require(goal)
        if self.expected_state is not None:
            goal.require().require_state(self.expected_state)


@dataclass(frozen=True)
class GoalActionContext:
    goals: GoalActionStore
    thread: Thread
    actor: type = RuntimeInvocable

    @property
    def reported_turn(self) -> ReportedTurnState:
        return self.thread.turn_record.read().reported_turn

    @property
    def model_report(self) -> bool:
        return self.actor.actor_reports_turn()


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
    def complete_wait(self, ctx: GoalActionContext) -> None:
        """Clear the goal's wait after apply; members that preserve it override."""
        ctx.thread.goal.clear_wait(ctx.goals.clear_goal_wait)

    @classmethod
    async def after_update(cls, scheduler: object, session_id: object) -> None:
        """Called after the goal update, on the action the scheduler chose and not on the
        instance it applied; members that schedule override."""


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

    def prepare(self, goals: GoalActionStore, name: str, actor: type) -> GoalAction[Result]:
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
        ctx.goals.commit_registration(ctx.edit, change)
        self.complete_wait(ctx)
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
        goal.state.check_transition(state, owner=ctx.actor.actor_is_owner())

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
    @classmethod
    async def after_update(cls, scheduler: object, session_id: object) -> None:
        await scheduler.schedule_goal(session_id)

    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def before_transition(self, goal: Goal, ctx: GoalActionContext) -> None:
        ctx.goals.resume_goal_generation(goal.id, ctx.thread.executing)


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
        ctx.goals.comply_goal_generation(
            goal.id, ComplianceDecision(self.witness), ctx.thread.executing,
        )


@dataclass(frozen=True, kw_only=True)
class StandbyGoalAction(GoalReportAction, ModelInvocable, RuntimeInvocable):
    wait_for: tuple[str, ...] = ()
    reviewed_inputs: tuple[Annotated[InputKey, InputKeyText], ...] = ()
    def complete_wait(self, ctx: GoalActionContext) -> None:
        pass


    def next_state(self, goal: Goal, ctx: GoalActionContext) -> GoalState:
        return ActiveGoal()

    def prepare(self, goals: GoalActionStore, name: str, actor: type) -> GoalAction[Goal]:
        ctx = GoalActionContext(goals, goals.require_thread(name), actor)
        self.expect.check(ctx)
        review = goals.prepare_standby_review(
            ctx.thread, ctx.thread.goal.require().id, self.wait_for,
        )
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

    review: PreparedStandbyReview

    def before_goal_publish(self, goal: Goal, ctx: GoalActionContext) -> None:
        self.review.commit_standby(
            goal, ctx.thread, ctx.reported_turn, self.wait_for, self.reviewed_inputs,
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
        ctx.goals.retire_goal_generation(goal.id)


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
            published_seq=ctx.goals.bus_revision(),
        )
        goal = replace(
            goal,
            mention_source=bind_goal_mentions(
                goal.text, goal.id, goal.revision, ctx.thread, ctx.goals.registry_snapshot()
            ),
        )
        ctx.goals.create_goal_generation(goal.id)
        return goal


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
    def complete_wait(self, ctx: GoalActionContext) -> None:
        pass


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
                text, goal.id, revision, ctx.thread, ctx.goals.registry_snapshot()
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
        ctx.goals.authorize_goal_retry(goal.id, RetryDecision(RetryDecisionId.mint()))
