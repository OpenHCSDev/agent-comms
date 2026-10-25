"""Goals: declaration and persistence owners."""

from __future__ import annotations

from agent_comms.lifecycle_capability import Active

import signal
import subprocess
import tempfile
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from .child_process import (BoundedRun, CheckPrinted, ChildOutcome, ChildResult, CountPrinted,
                            ExitedOutcome, SignaledOutcome, TimedOutOutcome)
from .context_segments.wake_values import NoWakeGoalContext, WakeGoalContext, WakeGoalValue
from .declared_family import DeclaredFamily
from .errors import RejectedEnforcementError, RelationViolationError
from .field_codec import (
    GoalId,
    FieldCodec,
    MalformedRecord,
    NullMember,
    PathText,
    PresenceExclude,
    RecordMember,
    SubclassUnion,
    TurnId,
)
from .goal_states import ActiveGoal, GoalState
from agent_comms.goal_turn_permit import GoalTurnPermit, NoLaunchPermit
from .turn_goal_permission import ContinuationGoalPermission, GoalPermission, InactiveGoalPermission
from .registration_inheritance import RegistrationInheritance
from .repo import DetachedStart, GitCommandError, Repo, ScratchCheckout
from .shell_commands import ShellCommand
from .test_invocation import PYTEST
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .text_digest import TextDigest
from .reported_turn import NoReportedTurn, ReportedTurnState
from .ui_model.goal import GoalDisplay

if TYPE_CHECKING:
    from .goal_actions import GoalAction, GoalActionContext
    from .goal_presentation import GoalExecution
    from .goal_waits import GoalWaits
    from .task_sources import TaskScope
    from .threads import Thread
    from .turn_context import TurnContext

if TYPE_CHECKING:
    from .goal_management import Goals
    from .goal_attempts import GoalAttemptStore, LaunchPermit
    from .turn_goal_account import VerifiedGoalSettlement
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


class GoalChangedError(RejectedEnforcementError):
    """An observed goal changed before its checked action could run."""

    reason: ClassVar[str] = "goal changed"

    def __init__(self, thread: Thread, expected: GoalCheckpoint) -> None:
        self.thread = thread
        self.expected = expected
        super().__init__()

    def __str__(self) -> str:
        return (
            f"{self.thread.name}: observed {self.expected}, now {self.thread.goal.captured}; "
            "the action was not applied"
        )


class CheckResult(DeclaredFamily, affix="CheckResult"):
    def require(self, result: ChildResult) -> None:
        self.distance(result).require(self)

    @abstractmethod
    def distance(self, result: ChildResult) -> CheckPrinted:
        """The measured distance, or the run's declared missing-count state."""


@dataclass(frozen=True)
class SilentCheckResult(CheckResult):
    def distance(self, result: ChildResult) -> CheckPrinted:
        return CountPrinted(int(result.outcome != ExitedOutcome(0) or bool(result.stdout)))


@dataclass(frozen=True)
class CountCheckResult(CheckResult):
    expected: int

    def __post_init__(self):
        if self.expected < 0:
            raise ValueError("An expected count must be nonnegative.")

    def distance(self, result: ChildResult) -> CheckPrinted:
        return result.outcome.printed_count(result.stdout).distance(self.expected)


class CheckCommand:
    """Command validation shared by authored requests and stored checks."""

    command: str

    def __post_init__(self):
        if not self.command.strip():
            raise ValueError("An order check requires a command.")

    def require_writable(self) -> None:
        """New checks cannot select revisions; saved checks remain readable for correction."""
        PYTEST.require_integrator(self.command)
        if any(self.git_command(list(simple.words)) in self.REVISION_COMMANDS
               for simple in ShellCommand.split(self.command)):
            raise ValueError("A check never checks out a revision: Core runs it at the "
                             "delivered commit and at each branch commit. Drop the checkout.")

    # The revision is the run's parameter; a check line that chooses one is refused.
    REVISION_COMMANDS: ClassVar[frozenset[str]] = frozenset({"checkout", "switch"})
    @staticmethod
    def git_command(words: Sequence[str]) -> str:
        """The git subcommand ``words`` run, or "" when they run no git."""
        from .storage_rules import GitProgram

        if not words or Path(words[0]).name not in GitProgram.names:
            return ""
        invocation = GitProgram.invocation(list(words[1:]), Path.cwd())
        return next(iter(invocation.args), "")


@dataclass(frozen=True)
class OrderCheck(CheckCommand):
    directory: Annotated[Path, PathText]
    command: str
    result: CheckResult = field(default_factory=SilentCheckResult)
    timeout: ClassVar[int] = 300

    def registered(self, repos: tuple[Repo, ...]) -> OrderCheck:
        """The written directory names a registered repository, by name or checkout path; every
        reader runs in that repository's absolute checkout, whatever directory it was sent from."""
        return replace(self, directory=Repo.selected(repos, str(self.directory)).checkout)

    def require_writable(self) -> None:
        """A relative directory means the cwd of whoever reads it next: the connector's
        heartbeat, a unit's checkout, a commander in another shell. New checks bind it first."""
        super().require_writable()
        if not self.directory.is_absolute():
            raise ValueError(f"The order check directory {str(self.directory)!r} is relative; "
                             "name a registered repository so the sender binds its checkout.")

    def require_directory(self):
        if not self.directory.is_dir():
            raise ValueError("The order check directory does not exist.")

    def require_acceptance(self, base: str, checkout_base: Path) -> None:
        """The order state supplies the source preceding its delivered work."""
        self.require_writable()
        try:
            ran = self.run_at(base, self.directory, checkout_base)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f"The order check timed out on {base}.") from error
        if refused := self.result.distance(ran).unproven(self.command, 0):
            raise ValueError("; ".join(refused))

    async def run(self) -> ChildResult:
        """The check on its directory's live state."""
        PYTEST.require_integrator(self.command)
        return await BoundedRun.run(("bash", "-c", self.command), cwd=self.directory,
                                    timeout=self.timeout)

    def recorded(self, sha: str, repository: Path) -> tuple[ChildResult, ...]:
        """Read the check run result without starting the check."""
        return self.recorded_among(CheckedCommit.stored(repository, sha))

    def recorded_among(self, stored: tuple[CheckedCommit, ...]) -> tuple[ChildResult, ...]:
        """This check's result among a commit's stored check results."""
        return tuple(checked.result for checked in stored if checked.command == self.command)[:1]

    def at(self, revision: str, repository: Path, checkout_base: Path) -> ChildResult:
        """Read the result on ``revision``, or explicitly run the check in a detached checkout."""
        sha = Repo.git(repository, "rev-parse", f"{revision}^{{commit}}").strip()
        for recorded in self.recorded(sha, repository):
            return recorded
        try:
            result = self.run_at(sha, repository, checkout_base)
        except subprocess.TimeoutExpired:
            return ChildResult(TimedOutOutcome(SignaledOutcome(signal.SIGKILL)))
        checked = CheckedCommit(
            self.command, result.outcome, result.stdout.decode(errors="replace"))
        checked.store(repository, sha)
        return checked.result

    def run_at(self, revision: str, repository: Path, checkout_base: Path) -> ChildResult:
        """Run in a fresh detached tree, never in the configured checkout."""
        PYTEST.require_integrator(self.command)
        checkout_base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="order-check-", dir=checkout_base) as scratch:
            tree = str(Path(scratch) / "tree")
            checkout = ScratchCheckout.add(repository, tree, DetachedStart(revision))
            try:
                run = subprocess.run(("bash", "-c", self.command), cwd=tree, capture_output=True,
                                     timeout=self.timeout)
            finally:
                checkout.retire()
        return ChildResult(ChildOutcome.from_returncode(run.returncode), run.stdout)


@dataclass(frozen=True)
class CheckedCommit:
    """What an order's check printed at a commit. A commit's result never changes, so the git
    note that records it on the commit is read by every process before any check runs."""

    command: str
    outcome: ChildOutcome
    stdout: str

    NOTES: ClassVar[tuple[str, ...]] = (
        "-c", "user.name=agent-comms", "-c", "user.email=core@agent-comms.invalid",
        "notes", "--ref=agent-comms-checks")

    @property
    def result(self) -> ChildResult:
        return ChildResult(self.outcome, self.stdout.encode())

    @classmethod
    def stored(cls, repository: Path, sha: str) -> tuple[CheckedCommit, ...]:
        """Every check result the commit's note records; a commit no check ran on has none."""
        try:
            Repo.git(repository, *cls.NOTES, "list", sha)
        except GitCommandError as failure:
            if failure.no_check_ran_on(sha):
                return ()  # No check ran on this commit.
            raise
        return tuple(FieldCodec.decode(cls, FieldCodec.parse(line)) for line in
                     Repo.git(repository, *cls.NOTES, "show", sha).splitlines() if line)

    def store(self, repository: Path, sha: str) -> None:
        Repo.git(repository, *self.NOTES, "append", "-m",
                 FieldCodec.compact(FieldCodec.encode(self)), sha)


class GoalOrder(DeclaredFamily, affix="GoalOrder"):
    def require_completion(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        pass

    def instructions(self, todos: Sequence[Todo]) -> str:
        return ""

    def todos(self, goals: Goals, owner: ThreadIncarnation, goal_id: GoalId) -> tuple[Todo, ...]:
        return ()

    def goal_text(self, goal: Goal) -> str:
        return goal.text

    def progress(self, goal: Goal, todos: Sequence[Todo]) -> str:
        return goal.progress

    def require_model_block(self) -> None:
        pass

    @abstractmethod
    def require_order(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None: ...

    @abstractmethod
    def require_replacement(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None: ...

    def require_edit(self) -> None:
        """Editing keeps the goal's identity, order and directives, so only text an order
        derives itself refuses a new wording."""


@dataclass(frozen=True)
class UnorderedGoalOrder(GoalOrder):
    def require_order(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        pass

    def require_replacement(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        pass


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

    def todos(self, goals: Goals, owner: ThreadIncarnation, goal_id: GoalId) -> tuple[Todo, ...]:
        return goals.todos.for_goal(owner, goal_id)

    def instructions(self, todos: Sequence[Todo]) -> str:
        return "\n\n".join(todo.order.instructions(todo) for todo in todos if todo.pending)

    def goal_text(self, goal: Goal) -> str:
        return self.text

    def require_edit(self) -> None:
        raise ValueError("A problem Goal's text is derived from its assigned problems.")

    def progress(self, goal: Goal, todos: Sequence[Todo]) -> str:
        """Each problem's order state and latest verdict, as Core records them on its Todo."""
        return "\n".join(" · ".join((f"{todo.id}: {todo.order.line}",
                                     *(verdict.line for verdict in todo.verdicts[-1:])))
                         for todo in todos)

    def require_model_block(self) -> None:
        raise ValueError("A problem goal is not blocked by text: ask the commander to block the "
                         "problem's Todo, or standby with wait_for naming the decider.")


@dataclass(frozen=True)
class GoalAuthority:
    """What an owner input was granted: this goal in this state.

    Assigning or replacing a problem order revises the goal but keeps its authority, and an
    order or retry that makes the same goal active keeps a running turn; pausing, completing,
    blocking, clearing or replacing the goal ends it.
    """

    goal_id: GoalId
    state: GoalState

    def keeps_turn_under(self, current: GoalAuthority | NoGoal) -> bool:
        """Whether a turn granted this authority may keep working under the current goal."""
        return current.continues(self)

    def continues(self, captured: GoalAuthority) -> bool:
        return self.goal_id == captured.goal_id and self.state.continues_turn_from(captured.state)


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
    def following_permission(self) -> GoalPermission: ...

    @property
    @abstractmethod
    def authority(self) -> GoalAuthority | NoGoal: ...

    @abstractmethod
    def task_scope(self, scope: TaskScope) -> TaskScope: ...

    @abstractmethod
    def add_context(self, context: TurnContext, owner: Thread, goals: Goals) -> TurnContext: ...

    @abstractmethod
    def peer_change_values(self) -> GoalPeerChange: ...

    @abstractmethod
    def stopped_permit(self, store: GoalAttemptStore) -> GoalTurnPermit: ...

    @abstractmethod
    def settle(self, settlement: type[VerifiedGoalSettlement], store: GoalAttemptStore,
               permit: LaunchPermit, turn: TurnId) -> None: ...

    @abstractmethod
    def instructions_for(self, goals: Goals, owner: ThreadIncarnation) -> tuple[str, ...]: ...

    def check_expected(self, current: GoalDeclaration) -> None:
        pass

    def accepts_following_handoff(self) -> bool:
        return False

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
    def wake_context(self, goals: Goals, owner: ThreadIncarnation) -> WakeGoalValue: ...

    def continued_by(self, current: GoalDeclaration) -> GoalDeclaration:
        return self

    def require(self) -> Goal:
        raise RelationViolationError("No goal is set for this thread.")

    @abstractmethod
    def reported_id(self) -> GoalId | None: ...

    @abstractmethod
    def clear_wait(self, waits: GoalWaits, *, wait_id: str | None = None) -> bool: ...

    def require_active(self) -> Goal:
        goal = self.require()
        goal.state.require(Active, RelationViolationError(goal.state.activity_refusal()))
        return goal

    def require_checkpoint(self, checkpoint: GoalRevision) -> Goal:
        if not PresentGoalCheckpoint(checkpoint).matches(self):
            raise ValueError("The goal changed before this action; the action was not applied.")
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
class NoGoal(NullMember, GoalDeclaration):
    def stopped_permit(self, store: GoalAttemptStore) -> GoalTurnPermit:
        return NoLaunchPermit()

    def __iter__(self) -> Iterator[Goal]:
        return iter(())

    def apply_action[R: GoalDeclaration](self, action: GoalAction[R], ctx: GoalActionContext) -> R:
        return action.create(ctx)

    def registration_value(self, previous: GoalDeclaration) -> GoalDeclaration:
        return previous

    def settle(self, settlement: type[VerifiedGoalSettlement], store: GoalAttemptStore,
               permit: LaunchPermit, turn: TurnId) -> None:
        pass

    def instructions_for(self, goals: Goals, owner: ThreadIncarnation) -> tuple[str, ...]:
        return ()

    def reported_id(self) -> None:
        return None

    def clear_wait(self, waits: GoalWaits, *, wait_id: str | None = None) -> bool:
        return False

    def wake_context(self, goals: Goals, owner: ThreadIncarnation) -> WakeGoalValue:
        return NoWakeGoalContext()

    shows_document: ClassVar[bool] = False
    shows_progress: ClassVar[bool] = False

    def following_permission(self) -> GoalPermission:
        return InactiveGoalPermission()

    @property
    def authority(self) -> NoGoal:
        return self

    def keeps_turn_under(self, current: GoalAuthority | NoGoal) -> bool:
        return current == self

    def continues(self, captured: GoalAuthority) -> bool:
        return False

    def task_scope(self, scope: TaskScope) -> TaskScope:
        return scope

    def add_context(self, context: TurnContext, owner: Thread, goals: Goals) -> TurnContext:
        return context

    def peer_change_values(self) -> GoalPeerChange:
        return GoalPeerChange(goal=self, state=self, progress="")

    can_control: ClassVar[bool] = False
    visible: ClassVar[bool] = False
    objective: ClassVar[str] = ""
    progress: ClassVar[str] = ""

    def standby(self, execution: GoalExecution) -> bool:
        return False

    def heading(self, execution: GoalExecution) -> str:
        return "No current goal"

    def action_failure(self, error: Exception) -> str:
        return "This goal has been cleared."


@dataclass(frozen=True)
class Goal(RecordMember, GoalDeclaration):
    """A goal owns one typed record for storage and agent-comms protocol boundaries."""

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

    def stopped_permit(self, store: GoalAttemptStore) -> GoalTurnPermit:
        return store.stopped_permit(self.id)

    def __iter__(self) -> Iterator[Goal]:
        yield self

    def apply_action[R: GoalDeclaration](self, action: GoalAction[R], ctx: GoalActionContext) -> R:
        return action.change(self, ctx)

    def registration_value(self, previous: GoalDeclaration) -> GoalDeclaration:
        return self

    def settle(self, settlement: type[VerifiedGoalSettlement], store: GoalAttemptStore,
               permit: LaunchPermit, turn: TurnId) -> None:
        settlement(store, permit, self, turn).dispatch_sync(self.state)

    def instructions_for(self, goals: Goals, owner: ThreadIncarnation) -> tuple[str, ...]:
        return (self.render_instructions(self.order.instructions(
            self.order.todos(goals, owner, self.id))),)

    def reported_id(self) -> GoalId:
        return self.id

    def clear_wait(self, waits: GoalWaits, *, wait_id: str | None = None) -> bool:
        return waits.clear(self.id, wait_id=wait_id)

    def wake_context(self, goals: Goals, owner: ThreadIncarnation) -> WakeGoalValue:
        todos = self.order.todos(goals, owner, self.id)
        return WakeGoalContext(
            text=self.order.goal_text(self), state=self.state,
            progress=self.order.progress(self, todos),
        )

    shows_document: ClassVar[bool] = True

    @property
    def shows_progress(self) -> bool:
        return bool(self.progress)

    def following_permission(self) -> GoalPermission:
        return ContinuationGoalPermission(self)

    @property
    def authority(self) -> GoalAuthority:
        return GoalAuthority(self.id, self.state)

    def task_scope(self, scope: TaskScope) -> TaskScope:
        return scope.with_goal(self.revision_key)

    def add_context(self, context: TurnContext, owner: Thread, goals: Goals) -> TurnContext:
        return context.prepend_goal(owner, self, goals)

    def peer_change_values(self) -> GoalPeerChange:
        return GoalPeerChange(goal=self.revision_key, state=type(self.state), progress=self.progress)

    def check_expected(self, current: GoalDeclaration) -> None:
        if self != current:
            raise ValueError("Goal changed during resume; refresh its state.")

    def accepts_following_handoff(self) -> bool:
        return bool(self.state.items_with(Active, self))

    @property
    def objective(self) -> str:
        return self.text

    def details_heading(self, execution: GoalExecution) -> str:
        return execution.details_heading(self.heading(execution))

    can_control: ClassVar[bool] = True
    visible: ClassVar[bool] = True

    def standby(self, execution: GoalExecution) -> bool:
        return execution.waiting_for(self)

    def heading(self, execution: GoalExecution) -> str:
        return execution.heading(self.revision)

    def action_failure(self, error: Exception) -> str:
        return str(error)

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
            raise ValueError(self.state.activity_refusal())

    def matching(self, goal_id: GoalId) -> GoalDeclaration:
        return self if self.id == goal_id else NoGoal()

    def active(self) -> GoalDeclaration:
        return next(iter(self.state.items_with(Active, self)), NoGoal())

    def reports_turn(self, turn: ReportedTurnState) -> bool:
        return self.reported_turn.reports(turn)

    @property
    def captured(self) -> GoalCheckpoint:
        return PresentGoalCheckpoint(self.revision_key)

    def render_instructions(self, orders_text: str) -> str:
        return "\n\n".join(part for part in (self.order.goal_text(self), orders_text) if part)

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
        return f"Goal · {self.state.declared_name}: {self.text}"
