from __future__ import annotations

from abc import abstractmethod
from collections.abc import (
    Callable,
    Iterator,
    Sequence,
)
from dataclasses import (
    dataclass,
    field,
)
from typing import Annotated, ClassVar, Literal

from metaclass_registry.mro_dispatch import MroDispatch

from agent_comms.errors import GoalRefused, RejectedEnforcementError, RelationViolationError
from agent_comms.execution.goal_turn_permit import GoalTurnPermit, NoLaunchPermit
from agent_comms.execution.reported_turn import NoReportedTurn, ReportedTurnState
from agent_comms.execution.turn_goal_permission import (
    ContinuationGoalPermission,
    GoalPermission,
    InactiveGoalPermission,
)
from agent_comms.nominal.compact_text import CompactText
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    FieldCodec,
    GoalId,
    MalformedRecord,
    NullMember,
    PresenceExclude,
    RecordMember,
    SubclassUnion,
    TurnId,
)
from agent_comms.nominal.lifecycle_capability import Active
from agent_comms.nominal.registration_inheritance import RegistrationInheritance
from agent_comms.nominal.text_digest import TextDigest
from agent_comms.planning.goal_authority import GoalAuthority, GoalTurnAuthority, NoGoalAuthority
from agent_comms.planning.goal_mutation import GoalMutation
from agent_comms.planning.goal_permit_source import GoalPermitSource
from agent_comms.planning.goal_states import ActiveGoal, GoalState
from agent_comms.planning.goal_work import (
    GoalAssignment,
    GoalObservation,
    GoalOwner,
    GoalWorkSource,
)
from agent_comms.registry.thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from agent_comms.transport.tool_result import DeclaredReply
from agent_comms.ui_model.goal import GoalDisplay, GoalExecutionDisplay


class OrphansHolds:
    """A holder that can never answer: the work it held is orphaned, and any teammate may
    release or withdraw it, as the REHOME line tells them to."""

    def require_hold_consent(self, require_consent: Callable[[], None]) -> None:
        pass


@dataclass(frozen=True)
class GoalUnavailable(GoalDisplay["GoalDeclaration", "Goal"]):
    last_confirmed: GoalDeclaration

    @property
    def can_control(self) -> bool:
        return False

    @property
    def snapshot(self) -> GoalDeclaration:
        return self.last_confirmed

    visible: ClassVar[bool] = True

    def standby(self, execution: GoalExecutionDisplay[Goal]) -> bool:
        return False

    def heading(self, execution: GoalExecutionDisplay[Goal]) -> str:
        return self.last_confirmed.unavailable_heading


type GoalContactFactory[Contact] = Callable[[str, float, str, float, GoalId, int], Contact]
type GoalDiagnosticFactory[Diagnostic] = Callable[
    [str, GoalId, int, str, type[GoalMentionBinding] | Literal["stale_incarnation"]], Diagnostic,
]


@dataclass(frozen=True, kw_only=True)
class GoalMentionBinding(DeclaredFamily, affix="MentionBinding"):
    """One current stored resolution family; each member owns its projection."""

    family_discriminator = "resolution"
    token: str

    def __post_init__(self):
        if not self.token:
            raise ValueError("Goal mention requires a token")

    @abstractmethod
    def project[Contact, Diagnostic](
        self, registry, owner, goal, source,
        make_contact: GoalContactFactory[Contact], make_diagnostic: GoalDiagnosticFactory[Diagnostic],
    ) -> tuple[tuple[Contact, ...], tuple[Diagnostic, ...]]: ...


@dataclass(frozen=True, kw_only=True)
class UnresolvedMention:
    """Shared diagnostic capability; never a separately registered state."""

    peer_name: Literal[None] = None
    peer_created_at: Literal[None] = None

    def project[Contact, Diagnostic](
        self, registry, owner, goal, source,
        make_contact: GoalContactFactory[Contact], make_diagnostic: GoalDiagnosticFactory[Diagnostic],
    ) -> tuple[tuple[Contact, ...], tuple[Diagnostic, ...]]:
        return (), (
            make_diagnostic(
                owner.name, goal.id, source.text_revision, self.token, type(self)
            ),
        )


@dataclass(frozen=True, kw_only=True)
class SelfMentionBinding(UnresolvedMention, GoalMentionBinding):
    pass


@dataclass(frozen=True, kw_only=True)
class UnknownMentionBinding(UnresolvedMention, GoalMentionBinding):
    pass


@dataclass(frozen=True, kw_only=True)
class AliasMentionBinding(UnresolvedMention, GoalMentionBinding):
    pass


@dataclass(frozen=True, kw_only=True)
class MalformedMentionBinding(UnresolvedMention, GoalMentionBinding):
    pass


@dataclass(frozen=True, kw_only=True)
class LimitExceededMentionBinding(UnresolvedMention, GoalMentionBinding):
    pass


@dataclass(frozen=True, kw_only=True)
class NonExecutableMentionBinding(UnresolvedMention, GoalMentionBinding):
    pass


@dataclass(frozen=True, kw_only=True)
class ResolvedMentionBinding(GoalMentionBinding):
    peer_name: str
    peer_created_at: float

    def __post_init__(self):
        super().__post_init__()
        RecordedThreadIncarnation(self.peer_name, self.peer_created_at)

    @property
    def peer(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.peer_name, self.peer_created_at)

    def project[Contact, Diagnostic](
        self, registry, owner, goal, source,
        make_contact: GoalContactFactory[Contact], make_diagnostic: GoalDiagnosticFactory[Diagnostic],
    ) -> tuple[tuple[Contact, ...], tuple[Diagnostic, ...]]:
        diagnostic = make_diagnostic(
            owner.name, goal.id, source.text_revision, self.token, "stale_incarnation"
        )
        if not self.peer.current(registry):
            return (), (diagnostic,)
        peer = registry.threads[self.peer.resolved(registry).name]
        contacts = tuple(
            make_contact(
                owner.name,
                owner.incarnation.wire_birth,
                executor.name,
                executor.incarnation.wire_birth,
                goal.id,
                source.text_revision,
            )
            for executor in peer.role.executor(peer)
            if executor.created_at != owner.created_at
        )
        return (contacts, ()) if contacts else ((), (diagnostic,))


@dataclass(frozen=True, slots=True)
class GoalMentionSource:
    """Text-revision and owner-incarnation proof saved with its registry Goal."""

    goal_id: GoalId
    text_revision: int
    text_digest: str
    owner_name: str
    owner_created_at: float
    bindings: tuple[GoalMentionBinding, ...]

    def __post_init__(self) -> None:
        GoalRevision(self.goal_id, self.text_revision)
        TextDigest(self.text_digest)
        RecordedThreadIncarnation(self.owner_name, self.owner_created_at)

    @property
    def revision_key(self) -> GoalRevision:
        return GoalRevision(self.goal_id, self.text_revision)

    @property
    def digest(self) -> TextDigest:
        return TextDigest(self.text_digest)

    @property
    def incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner_name, self.owner_created_at)

    def matches(self, goal, owner, registry) -> bool:
        if self.goal_id != goal.id or self.text_revision > goal.revision:
            return False
        return (
            self.digest.matches(goal.text)
            and self.incarnation.resolved(registry) == owner.incarnation
        )


@dataclass(frozen=True)
class GoalRevision:
    """A particular revision, independent of text and execution state."""

    id: GoalId
    revision: int

    def __post_init__(self) -> None:
        try:
            revision = FieldCodec.decode(int, self.revision)
        except MalformedRecord as error:
            raise ValueError(
                "Goal revision must be an exact nonnegative 63-bit integer."
            ) from error
        if not 0 <= revision < 1 << 63:
            raise ValueError("Goal revision must be an exact nonnegative 63-bit integer.")


class GoalCheckpoint(DeclaredFamily, affix="GoalCheckpoint"):
    """Captured goal identity, including observed absence or no observation."""

    def matches(self, goal: GoalDeclaration) -> bool:
        return self == goal.captured

    @abstractmethod
    def require_identity(self, goal_id: GoalId) -> None: ...

    @abstractmethod
    def __str__(self) -> str: ...


@dataclass(frozen=True)
class UnobservedGoalCheckpoint(GoalCheckpoint):
    def matches(self, goal: GoalDeclaration) -> bool:
        return True

    def require_identity(self, goal_id: GoalId) -> None:
        raise ValueError("No goal execution identity was observed")

    def __str__(self) -> str:
        return "unobserved goal"


@dataclass(frozen=True)
class AbsentGoalCheckpoint(GoalCheckpoint, declared_name="absent"):
    def require_identity(self, goal_id: GoalId) -> None:
        raise ValueError("Goal execution identity does not match its declaration")

    def __str__(self) -> str:
        return "no goal"


@dataclass(frozen=True)
class PresentGoalCheckpoint(GoalCheckpoint, declared_name="present"):
    revision: GoalRevision

    def require_identity(self, goal_id: GoalId) -> None:
        if self.revision.id != goal_id:
            raise ValueError("Goal execution identity does not match its declaration")

    def __str__(self) -> str:
        return f"goal {self.revision.id}@{self.revision.revision}"


class GoalConsentRequired(GoalRefused, RejectedEnforcementError):
    """A fresh goal observation does not authorize replacing its owner's work."""

    reason: ClassVar[str] = "goal owner approval required"

    def __init__(self, owner: GoalOwner, actor: ThreadIncarnation) -> None:
        super().__init__(
            f"{owner.name} already has {owner.goal.captured}; {actor.name} cannot assign "
            "new work without that goal owner's approval."
        )


class GoalChangedError(GoalRefused, RejectedEnforcementError):
    """An observed goal changed before its checked action could run."""

    reason: ClassVar[str] = "goal changed"

    def __init__(self, thread: GoalOwner, expected: GoalCheckpoint) -> None:
        super().__init__(
            f"{thread.name}: observed {expected}, now {thread.goal.captured}; "
            "the action was not applied"
        )


class GoalOrder(DeclaredFamily, affix="GoalOrder"):
    def require_completion(self, goal: Goal, goals: GoalWorkSource, owner: ThreadIncarnation) -> None:
        pass

    def instructions(self, todos: Sequence[GoalAssignment]) -> str:
        return ""

    def todos(self, goals: GoalWorkSource, owner: ThreadIncarnation, goal_id: GoalId) -> tuple[GoalAssignment, ...]:
        return ()

    def goal_text(self, goal: Goal, todos: Sequence[GoalAssignment] = ()) -> str:
        del todos
        return goal.text

    def progress(self, goal: Goal, todos: Sequence[GoalAssignment]) -> str:
        return goal.progress

    def require_model_block(self) -> None:
        pass

    @abstractmethod
    def require_order(self, goal: Goal, goals: GoalWorkSource, owner: ThreadIncarnation) -> None: ...

    @abstractmethod
    def require_replacement(self, goal: Goal, goals: GoalWorkSource, owner: ThreadIncarnation) -> None: ...

    def require_edit(self) -> None:
        """Editing keeps the goal's identity, order and directives, so only text an order
        derives itself refuses a new wording."""


@dataclass(frozen=True)
class UnorderedGoalOrder(GoalOrder):
    def require_order(self, goal: Goal, goals: GoalWorkSource, owner: ThreadIncarnation) -> None:
        pass

    def require_replacement(self, goal: Goal, goals: GoalWorkSource, owner: ThreadIncarnation) -> None:
        pass


@dataclass(frozen=True)
class ProblemGoalOrder(GoalOrder):
    """A unit's one Goal derives its text and instructions from its assigned Todos."""

    @classmethod
    def derived_text(cls, todos: Sequence[GoalAssignment]) -> str:
        """Derive real goal text from assigned problems rather than a generic placeholder."""
        lines = tuple(summary for todo in todos if (summary := todo.goal_summary().strip()))
        if not lines:
            raise ValueError("A problem Goal requires at least one assigned problem.")
        return "\n".join(lines)

    def require_completion(self, goal: Goal, goals: GoalWorkSource, owner: ThreadIncarnation) -> None:
        """Every assigned problem is delivered or closed; the Goal then completes.

        A problem Goal used to refuse completion outright, so the only way out of one was to
        replace it. Its problems are the condition: while one is still pending the Goal stays
        open, and once none is, the unit's work is done and the Goal says so.
        """
        if goals.todos.for_goal(owner, goal.id):
            raise GoalRefused("Complete each problem with its checked delivered commit.")

    def require_order(self, goal: Goal, goals: GoalWorkSource, owner: ThreadIncarnation) -> None:
        if not goals.todos.closed_for_goal(owner, goal.id):
            raise ValueError("The problem Goal has no assigned Todos.")

    def require_replacement(self, goal: Goal, goals: GoalWorkSource, owner: ThreadIncarnation) -> None:
        if goals.todos.holds_pending(owner):
            raise GoalRefused("Finish or release the assigned problems before replacing this Goal.")

    def todos(self, goals: GoalWorkSource, owner: ThreadIncarnation, goal_id: GoalId) -> tuple[GoalAssignment, ...]:
        return goals.todos.for_goal(owner, goal_id)

    def instructions(self, todos: Sequence[GoalAssignment]) -> str:
        return "\n\n".join(text for todo in todos for text in todo.goal_instructions())

    def goal_text(self, goal: Goal, todos: Sequence[GoalAssignment] = ()) -> str:
        lines = tuple(summary for todo in todos if (summary := todo.goal_summary().strip()))
        return "\n".join(lines) if lines else goal.text

    def require_edit(self) -> None:
        raise GoalRefused("A problem Goal's text is derived from its assigned problems.")

    def progress(self, goal: Goal, todos: Sequence[GoalAssignment]) -> str:
        """The owner's current report, followed by each problem's recorded order state."""
        order_progress = "\n".join(todo.progress_line() for todo in todos)
        return "\n\n".join(part for part in (goal.progress, order_progress) if part)

    def require_model_block(self) -> None:
        raise GoalRefused("A problem goal is not blocked by text: ask the teammate to block the "
                          "problem's Todo, or standby with wait_for naming the decider.")


@dataclass(frozen=True)
class GoalPeerChange:
    goal: GoalRevision | NoGoal
    state: type[GoalState] | NoGoal
    progress: str


class GoalDeclaration(
    GoalDisplay["GoalDeclaration", "Goal"], SubclassUnion,
    RegistrationInheritance["GoalDeclaration"], GoalObservation,
):
    """The recorded goal state, including a thread with no goal."""

    @classmethod
    def apply(cls, incoming: GoalDeclaration, previous: GoalDeclaration) -> GoalDeclaration:
        return incoming.registration_value(previous)

    @abstractmethod
    def registration_value(self, previous: GoalDeclaration) -> GoalDeclaration: ...

    @abstractmethod
    def __iter__(self) -> Iterator[Goal]: ...

    @abstractmethod
    def apply_action[Context, R: GoalDeclaration](
        self, action: GoalMutation[Goal, Context, R], ctx: Context,
    ) -> R: ...

    @abstractmethod
    def following_permission(self) -> GoalPermission: ...

    @property
    @abstractmethod
    def authority(self) -> GoalTurnAuthority: ...

    @abstractmethod
    def task_scope[Scope](
        self, scope: Scope, scoped: Callable[[GoalRevision], Scope],
    ) -> Scope: ...

    @abstractmethod
    def add_context[Context](
        self, context: Context, prepend_goal: Callable[[Goal], Context],
    ) -> Context: ...

    @abstractmethod
    def peer_change_values(self) -> GoalPeerChange: ...

    @abstractmethod
    def stopped_permit(self, store: GoalPermitSource) -> GoalTurnPermit: ...

    @abstractmethod
    def settle[Store, Permit](
        self, settlement: Callable[[Store, Permit, Goal, TurnId], MroDispatch],
        store: Store, permit: Permit, turn: TurnId,
    ) -> None: ...

    @abstractmethod
    def instructions_for(self, goals: GoalWorkSource, owner: ThreadIncarnation) -> tuple[str, ...]: ...

    def accepts_following_handoff(self) -> bool:
        return False

    def require_work(self, actor: ThreadIncarnation, owner: GoalOwner) -> None:
        """No goal occupies this ready thread's work slot."""

    def work_candidates[Owner](self, thread: Owner) -> tuple[Owner, ...]:
        return (thread,)

    def require_hold_consent(self, require_consent: Callable[[], None]) -> None:
        """A current or unavailable goal preserves its holder's assignment consent."""
        require_consent()

    @property
    def snapshot(self) -> GoalDeclaration:
        return self

    @property
    @abstractmethod
    def shows_document(self) -> bool: ...

    @property
    @abstractmethod
    def shows_progress(self) -> bool: ...

    @property
    @abstractmethod
    def objective(self) -> str: ...

    @property
    @abstractmethod
    def progress(self) -> str: ...

    unavailable_heading: ClassVar[str] = "Goal state unavailable"

    def continued_by(self, current: GoalDeclaration) -> GoalDeclaration:
        return self

    def require(self) -> Goal:
        raise RelationViolationError("No goal is set for this thread.")

    @abstractmethod
    def reported_id(self) -> GoalId | None: ...

    @abstractmethod
    def clear_wait(self, clear: Callable[[GoalId], bool]) -> bool: ...

    def require_active(self) -> Goal:
        goal = self.require()
        goal.state.require(Active, RelationViolationError(goal.state.activity_refusal()))
        return goal

    def require_checkpoint(self, checkpoint: GoalRevision) -> Goal:
        if not PresentGoalCheckpoint(checkpoint).matches(self):
            raise GoalRefused("The goal changed before this action; the action was not applied.")
        return self.require()

    def matching(self, goal_id: GoalId) -> GoalDeclaration:
        return self

    def active(self) -> GoalDeclaration:
        return self

    def accepts_observation(self, goal_id: GoalId, minimum_revision: int) -> bool:
        return False

    def reports_turn(self, turn: ReportedTurnState) -> bool:
        return False

    @property
    def captured(self) -> GoalCheckpoint:
        return AbsentGoalCheckpoint()


@dataclass(frozen=True)
class NoGoal(CompactText, OrphansHolds, NullMember, GoalDeclaration):
    def compact_text(self) -> str:
        return "No goal"

    def stopped_permit(self, store: GoalPermitSource) -> GoalTurnPermit:
        return NoLaunchPermit()

    def __iter__(self) -> Iterator[Goal]:
        return iter(())

    def apply_action[Context, R: GoalDeclaration](
        self, action: GoalMutation[Goal, Context, R], ctx: Context,
    ) -> R:
        return action.create(ctx)

    def registration_value(self, previous: GoalDeclaration) -> GoalDeclaration:
        return previous

    def settle[Store, Permit](
        self, settlement: Callable[[Store, Permit, Goal, TurnId], MroDispatch],
        store: Store, permit: Permit, turn: TurnId,
    ) -> None:
        pass

    def instructions_for(self, goals: GoalWorkSource, owner: ThreadIncarnation) -> tuple[str, ...]:
        return ()

    def reported_id(self) -> None:
        return None

    def clear_wait(self, clear: Callable[[GoalId], bool]) -> bool:
        return False

    shows_document: ClassVar[bool] = False
    shows_progress: ClassVar[bool] = False

    def following_permission(self) -> GoalPermission:
        return InactiveGoalPermission()

    @property
    def authority(self) -> NoGoalAuthority:
        return NoGoalAuthority()

    def task_scope[Scope](
        self, scope: Scope, scoped: Callable[[GoalRevision], Scope],
    ) -> Scope:
        return scope

    def add_context[Context](
        self, context: Context, prepend_goal: Callable[[Goal], Context],
    ) -> Context:
        return context

    def peer_change_values(self) -> GoalPeerChange:
        return GoalPeerChange(goal=self, state=self, progress="")

    @property
    def can_control(self) -> bool:
        return False

    visible: ClassVar[bool] = False
    objective: ClassVar[str] = ""
    progress: ClassVar[str] = ""

    def standby(self, execution: GoalExecutionDisplay[Goal]) -> bool:
        return False

    def heading(self, execution: GoalExecutionDisplay[Goal]) -> str:
        return "No current goal"


@dataclass(frozen=True)
class Goal(CompactText, RecordMember, GoalDeclaration):
    """A goal owns one typed record for storage and agent-comms protocol boundaries."""

    def compact_text(self) -> str:
        state = self.state.declared_name
        line = f"Goal [{state}] rev {self.revision}: {self.text}"
        if self.progress:
            return f"{line}\nProgress: {self.progress}"
        return line

    # A presence row names the goal and its state; its body and bookkeeping stay with the goal tools.
    text: Annotated[str, PresenceExclude()]
    id: GoalId
    published_seq: Annotated[int, PresenceExclude()]
    progress: Annotated[str, PresenceExclude()] = ""
    revision: Annotated[int, PresenceExclude()] = 0
    reported_turn: Annotated[ReportedTurnState, PresenceExclude()] = field(
        default_factory=NoReportedTurn)
    mention_source: Annotated[GoalMentionSource | None, PresenceExclude()] = None
    state: GoalState = field(default_factory=ActiveGoal)
    order: Annotated[GoalOrder, PresenceExclude()] = field(default_factory=UnorderedGoalOrder)

    def stopped_permit(self, store: GoalPermitSource) -> GoalTurnPermit:
        return store.stopped_permit(self.id)

    def __iter__(self) -> Iterator[Goal]:
        yield self

    def apply_action[Context, R: GoalDeclaration](
        self, action: GoalMutation[Goal, Context, R], ctx: Context,
    ) -> R:
        return action.change(self, ctx)

    def registration_value(self, previous: GoalDeclaration) -> GoalDeclaration:
        return self

    def settle[Store, Permit](
        self, settlement: Callable[[Store, Permit, Goal, TurnId], MroDispatch],
        store: Store, permit: Permit, turn: TurnId,
    ) -> None:
        settlement(store, permit, self, turn).dispatch_sync(self.state)

    def instructions_for(self, goals: GoalWorkSource, owner: ThreadIncarnation) -> tuple[str, ...]:
        todos = self.order.todos(goals, owner, self.id)
        return (self.render_instructions(self.order.instructions(todos), todos),)

    def reported_id(self) -> GoalId:
        return self.id

    def held_stop(self) -> tuple[str, ...]:
        return self.state.held_stop(self.order.goal_text(self))

    def clear_wait(self, clear: Callable[[GoalId], bool]) -> bool:
        return clear(self.id)

    shows_document: ClassVar[bool] = True

    @property
    def shows_progress(self) -> bool:
        return bool(self.progress)

    def following_permission(self) -> GoalPermission:
        return ContinuationGoalPermission(self.id)

    @property
    def authority(self) -> GoalAuthority:
        return GoalAuthority(self.id, self.state)

    def task_scope[Scope](
        self, scope: Scope, scoped: Callable[[GoalRevision], Scope],
    ) -> Scope:
        return scoped(self.revision_key)

    def add_context[Context](
        self, context: Context, prepend_goal: Callable[[Goal], Context],
    ) -> Context:
        return prepend_goal(self)

    def peer_change_values(self) -> GoalPeerChange:
        return GoalPeerChange(goal=self.revision_key, state=type(self.state), progress=self.progress)

    def accepts_following_handoff(self) -> bool:
        return bool(self.state.items_with(Active, self))

    def require_work(self, actor: ThreadIncarnation, owner: GoalOwner) -> None:
        self.state.require_work(actor, owner)

    def work_candidates[Owner](self, thread: Owner) -> tuple[Owner, ...]:
        return self.state.work_candidates(thread)

    @property
    def objective(self) -> str:
        return self.text

    def details_heading(self, execution: GoalExecutionDisplay[Goal]) -> str:
        return execution.details_heading(self.heading(execution))

    @property
    def can_control(self) -> bool:
        return True

    visible: ClassVar[bool] = True

    def standby(self, execution: GoalExecutionDisplay[Goal]) -> bool:
        return execution.waiting_for(self)

    def heading(self, execution: GoalExecutionDisplay[Goal]) -> str:
        return execution.heading(self.revision)

    unavailable_heading: ClassVar[str] = "Goal state unavailable · last confirmed snapshot"

    def continued_by(self, current: GoalDeclaration) -> GoalDeclaration:
        return current.matching(self.id)

    def require(self) -> Goal:
        return self

    def require_id(self, goal_id: GoalId) -> Goal:
        if self.id != goal_id:
            raise RelationViolationError("This goal was replaced or cleared; refresh its state.")
        return self

    def require_state(self, expected: GoalState) -> None:
        if self.state != expected:
            raise GoalRefused(self.state.activity_refusal())

    def matching(self, goal_id: GoalId) -> GoalDeclaration:
        return self if self.id == goal_id else NoGoal()

    def active(self) -> GoalDeclaration:
        return next(iter(self.state.items_with(Active, self)), NoGoal())

    def reports_turn(self, turn: ReportedTurnState) -> bool:
        return self.reported_turn.reports(turn)

    @property
    def captured(self) -> GoalCheckpoint:
        return PresentGoalCheckpoint(self.revision_key)

    def render_instructions(
        self, orders_text: str, todos: Sequence[GoalAssignment] = (),
    ) -> str:
        return "\n\n".join(part for part in (self.order.goal_text(self, todos), orders_text) if part)

    def __post_init__(self) -> None:
        if not self.text.strip() or not self.id:
            raise ValueError("A goal requires text and an identity.")
        GoalRevision(self.id, self.revision)
        if self.published_seq < 0:
            raise ValueError("Goal publication requires a nonnegative bus sequence.")

    @property
    def revision_key(self) -> GoalRevision:
        return GoalRevision(self.id, self.revision)

    def accepts_observation(self, goal_id: GoalId, minimum_revision: int) -> bool:
        """Later revisions may describe the same failed goal, never a replacement."""
        return self.id == goal_id and self.revision >= minimum_revision

    @property
    def summary(self) -> str:
        return f"Goal · {self.state.display_label()}: {self.text}"


@dataclass(frozen=True)
class GoalReply(DeclaredReply):
    goal: Goal
