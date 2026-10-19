"""Goals: declaration and persistence owners."""

from __future__ import annotations

import re
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from .child_process import ExitedOutcome
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, MalformedRecord, NullMember, RecordMember, SubclassUnion
from .goal_states import ActiveGoal, GoalState
from .goal_failure_observation import FailedTurnProjection
from .turn_goal_permission import ContinuationGoalPermission, InactiveGoalPermission, TurnGoalPermission
from .registration_inheritance import RegistrationInheritance
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .text_digest import TextDigest
from .ui_model.goal import GoalDisplay

if TYPE_CHECKING:
    from .goal_actions import GoalAction, GoalActionContext
    from .goal_presentation import ThreadGoalExecution
    from .goal_waits import GoalWait, GoalWaits
    from .agent_activity import AgentActivity
    from .registry_document import RegistrySnapshot
    from .task_sources import TaskScope
    from .threads import Thread
    from .thread_status import ThreadStatus
    from .turn_context import TurnContext

if TYPE_CHECKING:
    from .goal_management import Goals
    from .goal_attempts import GoalAttemptStore, LaunchPermit
    from .turn_goal_account import VerifiedGoalSettlement
    from .thread_identity import TurnId
    from .todos import Todo


@dataclass(frozen=True, kw_only=True)
class GoalMentionBinding(DeclaredFamily, affix="MentionBinding"):
    """One current stored resolution family; each member owns its projection."""

    family_discriminator = "resolution"
    token: str

    def __post_init__(self):
        if not self.token:
            raise ValueError("Goal mention requires a token")

    @abstractmethod
    def project(self, registry, owner, goal, source): ...


@dataclass(frozen=True, kw_only=True)
class UnresolvedMention:
    """Shared diagnostic capability; never a separately registered state."""

    peer_name: Literal[None] = None
    peer_created_at: Literal[None] = None

    def project(self, registry, owner, goal, source):
        from .relationships import GoalMentionDiagnostic

        return (), (
            GoalMentionDiagnostic(
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

    def project(self, registry, owner, goal, source):
        from .relationships import GoalDerivedContact, GoalMentionDiagnostic

        diagnostic = GoalMentionDiagnostic(
            owner.name, goal.id, source.text_revision, self.token, "stale_incarnation"
        )
        if not self.peer.current(registry):
            return (), (diagnostic,)
        peer = registry.threads[self.peer.resolved(registry).name]
        contacts = tuple(
            GoalDerivedContact(
                owner.name,
                owner.created_at,
                executor.name,
                executor.created_at,
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

    goal_id: str
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
    def checkpoint(self) -> GoalRevision:
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

    id: str
    revision: int

    def __post_init__(self) -> None:
        if not FieldCodec.decode(str, self.id):
            raise ValueError("Goal revision requires identity")
        try:
            revision = FieldCodec.decode(int, self.revision)
        except MalformedRecord as error:
            raise ValueError(
                "Goal revision must be an exact nonnegative 63-bit integer."
            ) from error
        if not 0 <= revision < 1 << 63:
            raise ValueError("Goal revision must be an exact nonnegative 63-bit integer.")


class GoalCheckpoint(DeclaredFamily, affix="GoalCheckpoint"):
    """Captured goal identity, not a goal record or execution permission."""

    @abstractmethod
    def require_identity(self, goal_id: str) -> None: ...


@dataclass(frozen=True)
class AbsentGoalCheckpoint(GoalCheckpoint, declared_name="absent"):
    def require_identity(self, goal_id: str) -> None:
        raise ValueError("Goal execution identity does not match its declaration")


@dataclass(frozen=True)
class PresentGoalCheckpoint(GoalCheckpoint, declared_name="present"):
    revision: GoalRevision

    def require_identity(self, goal_id: str) -> None:
        if self.revision.id != goal_id:
            raise ValueError("Goal execution identity does not match its declaration")


class CheckResult(DeclaredFamily, affix="CheckResult"):
    @abstractmethod
    def require(self, result) -> None: ...


@dataclass(frozen=True)
class SilentCheckResult(CheckResult):
    def require(self, result):
        if result.outcome != ExitedOutcome(0) or result.stdout:
            raise ValueError("The order check must print nothing and exit zero.")


@dataclass(frozen=True)
class CountCheckResult(CheckResult):
    expected: int

    def __post_init__(self):
        if self.expected < 0:
            raise ValueError("An expected count must be nonnegative.")

    def require(self, result):
        if not isinstance(result.outcome, ExitedOutcome):
            raise ValueError(f"The order check did not exit, so it printed no count of {self.expected}.")
        CheckPrinted.decoded(result.stdout).require(self.expected)


class CheckPrinted(DeclaredFamily, affix="Printed"):
    """What an order check printed: a count (its last integer) or no integer at all."""

    @staticmethod
    def decoded(stdout: bytes) -> CheckPrinted:
        counts = re.findall(r"\d+", stdout.decode(errors="replace"))
        return CountPrinted(int(counts[-1])) if counts else NoIntegerPrinted()

    @abstractmethod
    def require(self, expected: int) -> None: ...

    @abstractmethod
    def unproven(self, command: str, expected: int) -> tuple[str, ...]:
        """Why a check that must fail before any work proves nothing yet."""


@dataclass(frozen=True)
class CountPrinted(CheckPrinted):
    count: int

    def require(self, expected: int) -> None:
        if self.count != expected:
            raise ValueError(f"The order check printed {self.count}, not its expected count {expected}.")

    def unproven(self, command: str, expected: int) -> tuple[str, ...]:
        return ((f"Check already prints {self.count} before any work: `{command}` proves nothing; "
                 "write a check that fails now",) if self.count == expected else ())


@dataclass(frozen=True)
class NoIntegerPrinted(CheckPrinted):
    def require(self, expected: int) -> None:
        raise ValueError(f"The order check printed no integer, so no count of {expected}.")

    def unproven(self, command: str, expected: int) -> tuple[str, ...]:
        return ("the check printed no integer; use grep -c PATTERN FILE or ... | wc -l",)


@dataclass(frozen=True)
class OrderCheck:
    directory: str
    command: str
    result: CheckResult = field(default_factory=SilentCheckResult)

    def __post_init__(self):
        if not self.directory.strip() or not self.command.strip():
            raise ValueError("An order check requires a directory and command.")

    def require_directory(self):
        if not Path(self.directory).is_dir():
            raise ValueError("The order check directory does not exist.")

    LINE: ClassVar[re.Pattern] = re.compile(r"(\S+)\s*::\s*(.+?)\s*->\s*(\d+)")

    @classmethod
    def parse(cls, line: str) -> OrderCheck | None:
        """The one reading of a check line, `<repo> :: <shell command> -> <integer>`; else None."""
        found = cls.LINE.fullmatch(line.strip())
        return found and cls(found[1], found[2], CountCheckResult(int(found[3])))

class GoalOrder(DeclaredFamily, affix="GoalOrder"):
    def require_completion(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        pass

    def require_request(self) -> None:
        raise ValueError("An order serves an open owner request: request add records one.")

    def open_requests(
        self, goal: Goal, goals: Goals, owner: ThreadIncarnation
    ) -> tuple[tuple[Goal, tuple[Todo, ...]], ...]:
        return ()

    def instructions(self, todos: Sequence[Todo]) -> str:
        return ""

    def todos(self, goals: Goals, owner: ThreadIncarnation, goal_id: str) -> tuple[Todo, ...]:
        return ()

    def goal_text(self, goal: Goal) -> str:
        return goal.text

    def progress(self, goal: Goal, todos: Sequence[Todo]) -> str:
        return goal.progress

    def failed_turn_progress(self, goal: Goal, diagnostic: str) -> str:
        return f"{goal.progress}\n\n{diagnostic}" if goal.progress else diagnostic

    def require_model_block(self) -> None:
        pass

    @abstractmethod
    def require_order(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None: ...

    @abstractmethod
    def require_replacement(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None: ...


@dataclass(frozen=True)
class UnorderedGoalOrder(GoalOrder):
    def require_order(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        pass

    def require_replacement(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        pass


@dataclass(frozen=True)
class RequestGoalOrder(GoalOrder):
    """An owner request on the commander's own Goal: each order names the one it serves, and the
    request stays open until no order serving it is open."""

    def require_order(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        pass

    def require_replacement(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        if not goal.state.terminal:
            raise ValueError(f"Request {goal.id} is still open; mark it done first.")

    def require_completion(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        if self.serving(goal, goals, owner):
            raise ValueError(f"Request {goal.id} still has open orders.")

    def require_request(self) -> None:
        pass

    def serving(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> tuple[Todo, ...]:
        return tuple(todo for todo in goals.todos.for_request(owner, goal.id) if todo.pending)

    def open_requests(
        self, goal: Goal, goals: Goals, owner: ThreadIncarnation
    ) -> tuple[tuple[Goal, tuple[Todo, ...]], ...]:
        return ((goal, self.serving(goal, goals, owner)),)


@dataclass(frozen=True)
class ProblemGoalOrder(GoalOrder):
    """A unit's one Goal groups its canonical Todo assignments, not copied orders."""

    StrikeLimit: ClassVar[int] = 2  # live strikes the commanded unit may take on one problem
    text: ClassVar[str] = "Complete the assigned problems."

    def require_completion(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        raise ValueError("Complete each problem with its checked delivered commit.")

    def require_order(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        if not goals.todos.for_goal(owner, goal.id):
            raise ValueError("The problem Goal has no assigned Todos.")

    def require_replacement(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        if goals.todos.holds_pending(owner):
            raise ValueError("Finish or release the assigned problems before replacing this Goal.")

    def todos(self, goals: Goals, owner: ThreadIncarnation, goal_id: str) -> tuple[Todo, ...]:
        return goals.todos.for_goal(owner, goal_id)

    def instructions(self, todos: Sequence[Todo]) -> str:
        return "\n\n".join(todo.order.instructions(todo) for todo in todos if todo.pending)

    def goal_text(self, goal: Goal) -> str:
        return self.text

    def progress(self, goal: Goal, todos: Sequence[Todo]) -> str:
        """Each problem's order state and latest verdict, as Core records them on its Todo."""
        return "\n".join(" · ".join((f"{todo.id}: {todo.order.line}",
                                     *(verdict.line for verdict in todo.verdicts[-1:])))
                         for todo in todos)

    def failed_turn_progress(self, goal: Goal, diagnostic: str) -> str:
        return goal.progress

    def require_model_block(self) -> None:
        raise ValueError("A problem goal is not blocked by text: ask the commander to block the "
                         "problem's Todo, or standby with wait_for naming the decider.")


@dataclass(frozen=True)
class GoalPeerChange:
    goal: GoalRevision | NoGoal
    state: type[GoalState] | NoGoal
    progress: str


class GoalDeclaration(GoalDisplay, SubclassUnion, RegistrationInheritance["GoalDeclaration"]):
    """The recorded goal state, including a thread with no goal."""

    @classmethod
    def apply(cls, incoming: GoalDeclaration, previous: GoalDeclaration) -> GoalDeclaration:
        return incoming.registration_value(previous)

    @abstractmethod
    def registration_value(self, previous: GoalDeclaration) -> GoalDeclaration: ...

    @abstractmethod
    def __iter__(self) -> Iterator[Goal]: ...

    @abstractmethod
    def apply_action[R: GoalDeclaration](self, action: GoalAction[R], ctx: GoalActionContext) -> R: ...

    @abstractmethod
    def following_permission(self) -> TurnGoalPermission: ...

    @abstractmethod
    def task_scope(self, scope: TaskScope) -> TaskScope: ...

    @abstractmethod
    def add_context(self, context: TurnContext, owner: Thread, goals: Goals) -> TurnContext: ...

    @abstractmethod
    def describe_execution(self, wait: GoalWait, snapshot: RegistrySnapshot,
                           agents: AgentActivity) -> ThreadGoalExecution: ...

    @abstractmethod
    def peer_change_values(self) -> GoalPeerChange: ...

    @abstractmethod
    def failed_turn_projection(self, path: Path, owner: Thread, owner_status: ThreadStatus,
                               claim_scope: int) -> FailedTurnProjection: ...

    @abstractmethod
    def settle(self, settlement: type[VerifiedGoalSettlement], store: GoalAttemptStore,
               permit: LaunchPermit, turn: TurnId) -> None: ...

    @abstractmethod
    def block_after_failed_turn(self, goals: Goals, name: str, worktree: str,
                                diagnostic: str) -> GoalDeclaration: ...

    @abstractmethod
    def instructions_for(self, goals: Goals, owner: ThreadIncarnation) -> tuple[str, ...]: ...

    def after_failed_turn(self, diagnostic: str) -> GoalDeclaration:
        return self

    def check_expected(self, current: GoalDeclaration) -> None:
        pass

    def accepts_following_handoff(self) -> bool:
        return False

    def defers_following_to(self, current: GoalDeclaration) -> bool:
        return current.accepts_following_handoff()

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

    @abstractmethod
    def wake_context(self, goals: Goals, owner: ThreadIncarnation) -> dict[str, object] | None: ...

    def continued_by(self, current: GoalDeclaration) -> GoalDeclaration:
        return self

    def require(self) -> Goal:
        raise RelationViolationError("No goal is set for this thread.")

    @abstractmethod
    def reported_id(self) -> str | None: ...

    @abstractmethod
    def clear_wait(self, waits: GoalWaits, *, wait_id: str | None = None) -> bool: ...

    def require_active(self) -> Goal:
        goal = self.require()
        goal.state.require_active()
        return goal

    def require_checkpoint(self, checkpoint: GoalRevision) -> Goal:
        if self.captured != PresentGoalCheckpoint(checkpoint):
            raise ValueError("The goal changed before this action; the action was not applied.")
        return self.require()

    def matching(self, goal_id: str) -> GoalDeclaration:
        return self

    def active(self) -> GoalDeclaration:
        return self

    def accepts_observation(self, goal_id: str, minimum_revision: int) -> bool:
        return False

    def reports_turn(self, turn_id: str) -> bool:
        return False

    @property
    def captured(self) -> GoalCheckpoint:
        return AbsentGoalCheckpoint()


@dataclass(frozen=True)
class NoGoal(NullMember, GoalDeclaration):
    def __iter__(self) -> Iterator[Goal]:
        return iter(())

    def apply_action[R: GoalDeclaration](self, action: GoalAction[R], ctx: GoalActionContext) -> R:
        return action.create(ctx)

    def registration_value(self, previous: GoalDeclaration) -> GoalDeclaration:
        return previous

    def settle(self, settlement: type[VerifiedGoalSettlement], store: GoalAttemptStore,
               permit: LaunchPermit, turn: TurnId) -> None:
        pass

    def block_after_failed_turn(self, goals: Goals, name: str, worktree: str,
                                diagnostic: str) -> GoalDeclaration:
        return self

    def instructions_for(self, goals: Goals, owner: ThreadIncarnation) -> tuple[str, ...]:
        return ()

    def reported_id(self) -> None:
        return None

    def clear_wait(self, waits: GoalWaits, *, wait_id: str | None = None) -> bool:
        return False

    def wake_context(self, goals: Goals, owner: ThreadIncarnation) -> None:
        return None

    shows_document: ClassVar[bool] = False
    shows_progress: ClassVar[bool] = False

    def following_permission(self) -> TurnGoalPermission:
        return InactiveGoalPermission()

    def task_scope(self, scope: TaskScope) -> TaskScope:
        return scope

    def add_context(self, context: TurnContext, owner: Thread, goals: Goals) -> TurnContext:
        return context

    def describe_execution(self, wait: GoalWait, snapshot: RegistrySnapshot,
                           agents: AgentActivity) -> ThreadGoalExecution:
        return wait.idle_execution()

    def peer_change_values(self) -> GoalPeerChange:
        return GoalPeerChange(goal=self, state=self, progress="")

    def failed_turn_projection(self, path: Path, owner: Thread, owner_status: ThreadStatus,
                               claim_scope: int) -> FailedTurnProjection:
        return FailedTurnProjection("unavailable", "owner_or_goal_changed")

    can_control: ClassVar[bool] = False
    visible: ClassVar[bool] = False
    objective: ClassVar[str] = ""
    progress: ClassVar[str] = ""

    def standby(self, execution: ThreadGoalExecution) -> bool:
        return False

    def heading(self, execution: ThreadGoalExecution) -> str:
        return "No current goal"

    def action_failure(self, error: Exception) -> str:
        return "This goal has been cleared."


@dataclass(frozen=True)
class Goal(RecordMember, GoalDeclaration):
    """A goal owns one typed record for storage and agent-comms protocol boundaries."""

    text: str
    id: str
    published_seq: int
    progress: str = ""
    revision: int = 0
    reported_turn: str | None = None
    mention_source: GoalMentionSource | None = None
    state: GoalState = field(default_factory=ActiveGoal)
    order: GoalOrder = field(default_factory=UnorderedGoalOrder)

    def __iter__(self) -> Iterator[Goal]:
        yield self

    def apply_action[R: GoalDeclaration](self, action: GoalAction[R], ctx: GoalActionContext) -> R:
        return action.change(self, ctx)

    def registration_value(self, previous: GoalDeclaration) -> GoalDeclaration:
        return self

    def settle(self, settlement: type[VerifiedGoalSettlement], store: GoalAttemptStore,
               permit: LaunchPermit, turn: TurnId) -> None:
        settlement(store, permit, self, turn).dispatch_sync(self.state)

    def block_after_failed_turn(self, goals: Goals, name: str, worktree: str,
                                diagnostic: str) -> GoalDeclaration:
        return goals.block_goal_after_failed_turn(
            name, started_goal=self, expected_worktree=worktree, diagnostic=diagnostic,
        )

    def instructions_for(self, goals: Goals, owner: ThreadIncarnation) -> tuple[str, ...]:
        return (self.render_instructions(self.order.instructions(
            self.order.todos(goals, owner, self.id))),)

    def reported_id(self) -> str:
        return self.id

    def clear_wait(self, waits: GoalWaits, *, wait_id: str | None = None) -> bool:
        return waits.clear(self.id, wait_id=wait_id)

    def wake_context(self, goals: Goals, owner: ThreadIncarnation) -> dict[str, object]:
        todos = self.order.todos(goals, owner, self.id)
        return {"text": self.order.goal_text(self), "status": self.state.declared_name,
                "progress": self.order.progress(self, todos)}

    shows_document: ClassVar[bool] = True

    @property
    def shows_progress(self) -> bool:
        return bool(self.progress)

    def following_permission(self) -> TurnGoalPermission:
        return ContinuationGoalPermission(self)

    def task_scope(self, scope: TaskScope) -> TaskScope:
        return scope.with_goal(self.checkpoint)

    def add_context(self, context: TurnContext, owner: Thread, goals: Goals) -> TurnContext:
        return context.prepend_goal(owner, self, goals)

    def describe_execution(self, wait: GoalWait, snapshot: RegistrySnapshot,
                           agents: AgentActivity) -> ThreadGoalExecution:
        return wait.project(self, snapshot, agents)

    def peer_change_values(self) -> GoalPeerChange:
        return GoalPeerChange(goal=self.checkpoint, state=type(self.state), progress=self.progress)

    def failed_turn_projection(self, path: Path, owner: Thread, owner_status: ThreadStatus,
                               claim_scope: int) -> FailedTurnProjection:
        return FailedTurnProjection.read_for_goal(path, owner, owner_status, claim_scope, self)

    def check_expected(self, current: GoalDeclaration) -> None:
        if self != current:
            raise ValueError("Goal changed during resume; refresh its state.")

    def accepts_following_handoff(self) -> bool:
        return self.state.active

    def defers_following_to(self, current: GoalDeclaration) -> bool:
        return not self.state.active and super().defers_following_to(current)

    @property
    def objective(self) -> str:
        return self.text

    def details_heading(self, execution: ThreadGoalExecution) -> str:
        return execution.details_heading(self.heading(execution))

    can_control: ClassVar[bool] = True
    visible: ClassVar[bool] = True

    def standby(self, execution: ThreadGoalExecution) -> bool:
        return execution.waiting_for(self)

    def heading(self, execution: ThreadGoalExecution) -> str:
        status = "Standby" if self.standby(execution) else self.state.declared_name
        return f"Goal · {status} · rev {self.revision}"

    def action_failure(self, error: Exception) -> str:
        return str(error)

    unavailable_heading: ClassVar[str] = "Goal state unavailable · last confirmed snapshot"

    def continued_by(self, current: GoalDeclaration) -> GoalDeclaration:
        return current.matching(self.id)

    def require(self) -> Goal:
        return self

    def require_id(self, goal_id: str) -> Goal:
        if self.id != goal_id:
            raise RelationViolationError("This goal was replaced or cleared; refresh its state.")
        return self

    def require_state(self, expected: GoalState) -> None:
        if self.state != expected:
            raise ValueError(self.state.activity_refusal())

    def matching(self, goal_id: str) -> GoalDeclaration:
        return self if self.id == goal_id else NoGoal()

    def active(self) -> GoalDeclaration:
        return self if self.state.active else NoGoal()

    def reports_turn(self, turn_id: str) -> bool:
        return self.reported_turn == turn_id

    @property
    def captured(self) -> GoalCheckpoint:
        return PresentGoalCheckpoint(self.checkpoint)

    def render_instructions(self, orders_text: str) -> str:
        return "\n\n".join(part for part in (self.order.goal_text(self), orders_text) if part)

    def after_failed_turn(self, diagnostic: str) -> Goal:
        progress = self.order.failed_turn_progress(self, diagnostic)
        state = self.state.after_failed_turn(diagnostic)
        if state is self.state:
            return self
        return replace(self, state=state, progress=progress, revision=self.revision + 1)

    def __post_init__(self) -> None:
        if not self.text.strip() or not self.id:
            raise ValueError("A goal requires text and an identity.")
        GoalRevision(self.id, self.revision)
        if self.published_seq < 0:
            raise ValueError("Goal publication requires a nonnegative bus sequence.")
        if self.reported_turn is not None and not isinstance(self.reported_turn, str):
            raise ValueError("Goal reported turn must be a string or null.")

    @property
    def checkpoint(self) -> GoalRevision:
        return GoalRevision(self.id, self.revision)

    def accepts_observation(self, goal_id: str, minimum_revision: int) -> bool:
        """Later revisions may describe the same failed goal, never a replacement."""
        return self.id == goal_id and self.revision >= minimum_revision

    @property
    def summary(self) -> str:
        return f"Goal · {self.state.declared_name}: {self.text}"
