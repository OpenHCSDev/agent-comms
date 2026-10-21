"""Goals: declaration and persistence owners."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from .child_process import (BoundedRun, CheckPrinted, ChildOutcome, ChildResult, CountPrinted,
                            ExitedOutcome, SignaledOutcome, TimedOutOutcome)
from .context_segments.wake_values import NoWakeGoalContext, WakeGoalContext, WakeGoalValue
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import (
    FieldCodec, JsonShapeFamily, JsonShapeMember, MalformedRecord, NullMember,
    RecordMember, SubclassUnion,
)
from .goal_states import ActiveGoal, GoalState
from agent_comms.goal_turn_permit import GoalTurnPermit, NoLaunchPermit
from .goal_failure_observation import FailedTurnProjection
from .turn_goal_permission import ContinuationGoalPermission, GoalPermission, InactiveGoalPermission
from .registration_inheritance import RegistrationInheritance
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .text_digest import TextDigest
from .ui_model.goal import GoalDisplay

if TYPE_CHECKING:
    from .goal_actions import DirectiveGoalAction, GoalAction, GoalActionContext
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
    from .messages import Message
    from .private_bus_checkpoint import CheckedRead


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


@dataclass(frozen=True)
class OrderCheck:
    directory: str
    command: str
    result: CheckResult = field(default_factory=SilentCheckResult)

    def __post_init__(self):
        if not self.directory.strip() or not self.command.strip():
            raise ValueError("An order check requires a directory and command.")

    def require_writable(self) -> None:
        """New checks cannot select revisions; saved checks remain readable for correction."""
        if any(self.git_command(words) in self.REVISION_COMMANDS
               for words in self.simple_commands(self.command)):
            raise ValueError("A check never checks out a revision: Core runs it at the "
                             "delivered commit and at each branch commit. Drop the checkout.")

    def require_directory(self):
        if not Path(self.directory).is_dir():
            raise ValueError("The order check directory does not exist.")

    LINE: ClassVar[re.Pattern] = re.compile(r"(\S+)\s*::\s*(.+?)\s*->\s*(\d+)")
    # The revision is the run's parameter; a check line that chooses one is refused.
    REVISION_COMMANDS: ClassVar[frozenset[str]] = frozenset({"checkout", "switch"})
    SEPARATORS: ClassVar[frozenset[str]] = frozenset({"&&", "||", ";", "|", "&"})
    timeout: ClassVar[int] = 300

    @classmethod
    def parse(cls, line: str) -> OrderCheck | None:
        """The one reading of a check line, `<repo> :: <shell command> -> <integer>`; else None."""
        found = cls.LINE.fullmatch(line.strip())
        if not found:
            return None
        check = cls(found[1], found[2], CountCheckResult(int(found[3])))
        check.require_writable()
        return check

    @classmethod
    def simple_commands(cls, command: str) -> list[list[str]]:
        """The shell words of each simple command in ``command``."""
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        commands: list[list[str]] = [[]]
        for word in lexer:
            if word in cls.SEPARATORS:
                commands.append([])
            else:
                commands[-1].append(word)
        return commands

    @staticmethod
    def git_command(words: Sequence[str]) -> str:
        """The git subcommand ``words`` run, or "" when they run no git."""
        if words[:1] != ["git"]:
            return ""
        rest = iter(words[1:])
        for word in rest:
            if word in ("-C", "-c"):
                next(rest, "")
            elif not word.startswith("-"):
                return word
        return ""

    async def run(self) -> ChildResult:
        """The check on its directory's live state."""
        return await BoundedRun.run(("bash", "-c", self.command), cwd=self.directory,
                                    timeout=self.timeout)

    def recorded(self, sha: str, repository: str) -> tuple[ChildResult, ...]:
        """Read the check run result without starting the check."""
        return tuple(checked.result for checked in CheckedCommit.stored(repository, sha)
                     if checked.command == self.command)[:1]

    def at(self, revision: str, repository: str, checkout_base: Path) -> ChildResult:
        """Read the result on ``revision``, or explicitly run the check in a detached checkout."""
        sha = CheckedCommit.git(repository, "rev-parse", f"{revision}^{{commit}}").strip()
        for recorded in self.recorded(sha, repository):
            return recorded
        checkout_base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="order-check-", dir=checkout_base) as scratch:
            tree = str(Path(scratch) / "tree")
            CheckedCommit.git(repository, "worktree", "add", "--detach", tree, sha)
            try:
                run = subprocess.run(("bash", "-c", self.command), cwd=tree, capture_output=True,
                                     timeout=self.timeout)
            except subprocess.TimeoutExpired:
                return ChildResult(TimedOutOutcome(SignaledOutcome(signal.SIGKILL)))
            finally:
                CheckedCommit.git(repository, "worktree", "remove", "--force", tree)
        checked = CheckedCommit(self.command, ChildOutcome.from_returncode(run.returncode),
                                run.stdout.decode(errors="replace"))
        checked.store(repository, sha)
        return checked.result


class GitCommandFailed(ValueError):
    """The actual Git result, so its caller can recognize a command's absent state."""

    def __init__(self, run: subprocess.CompletedProcess[str]):
        self.run = run
        super().__init__(run.stderr.strip() or f"git failed with exit {run.returncode}")

    def source_path_absent(self, sha: str, file: str) -> bool:
        """Only Git's missing-path result, never a failed fetch or corrupt repository."""
        return self.run.returncode == 128 and self.run.stderr in (
            f"fatal: path '{file}' does not exist in '{sha}'\n",
            f"fatal: path '{file}' exists on disk, but not in '{sha}'\n",
        )

    def no_check_ran_on(self, sha: str) -> bool:
        """Git's absent-note reply, not a corrupt repository or an invalid revision."""
        return self.run.returncode == 1 and self.run.stderr in (
            "", f"error: no note found for object {sha}.\n",
        )


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

    @staticmethod
    def git(repository: str, *args: str) -> str:
        run = subprocess.run(("git", "-C", repository, *args), capture_output=True, text=True,
                             timeout=60, env=os.environ | {"LC_ALL": "C"})
        if run.returncode:
            raise GitCommandFailed(run)
        return run.stdout

    @classmethod
    def stored(cls, repository: str, sha: str) -> tuple[CheckedCommit, ...]:
        """Every check result the commit's note records; a commit no check ran on has none."""
        try:
            cls.git(repository, *cls.NOTES, "list", sha)
        except GitCommandFailed as failure:
            if failure.no_check_ran_on(sha):
                return ()  # No check ran on this commit.
            raise
        return tuple(FieldCodec.decode(cls, FieldCodec.parse(line)) for line in
                     cls.git(repository, *cls.NOTES, "show", sha).splitlines() if line)

    def store(self, repository: str, sha: str) -> None:
        self.git(repository, *self.NOTES, "append", "-m",
                 json.dumps(FieldCodec.encode(self), separators=(",", ":")), sha)


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

    def directed(self, goal: Goal, directive: DirectiveGoalAction, ctx: GoalActionContext) -> Goal:
        """Only a thread holding owner requests records a claude-ai directive."""
        return goal

    def directive_messages(self, goals: Goals) -> tuple[Message, ...]:
        return ()

    def overdue_directives(self, goal: Goal, goals: Goals, owner: ThreadIncarnation,
                           now: float) -> tuple[RequestDirective, ...]:
        return ()

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
class RequestDirective:
    """One original directive; only a reply linked to that message answers it."""

    window: ClassVar[int] = 10 * 60  # seconds the commander has to act on a directive
    seq: int
    deadline: float

    @property
    def due_time(self) -> str:
        return time.strftime("%H:%M", time.localtime(self.deadline))

    def original(self, goals: Goals) -> Message:
        source: CheckedRead
        with goals.bus.log.checked_read() as source:
            return source.delivery(self.seq).message

    def overdue(self, goals: Goals, owner: ThreadIncarnation, now: float) -> bool:
        if self.deadline >= now:
            return False
        original = self.original(goals).reference
        with goals.bus.log.checked_read() as source:
            replies = source.replies((original,))
        return not any(reply.message.sender == owner.name and original in reply.message.answers
                       for reply in replies)


@dataclass(frozen=True)
class RequestGoalOrder(GoalOrder):
    """An owner request on the commander's own Goal: each order names the one it serves, and the
    request stays open until no order serving it is open. Each claude-ai directive the commander
    receives is recorded on it when it arrives."""

    directives: tuple[RequestDirective, ...] = ()

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

    def directed(self, goal: Goal, directive: DirectiveGoalAction, ctx: GoalActionContext) -> Goal:
        """A closed request is replaced by one the directive opens; an open one records it."""
        if goal.state.terminal:
            return directive.request(goal, ctx)
        return replace(goal, revision=goal.revision + 1, order=replace(
            self, directives=(*self.directives, directive.directive())))

    def directive_messages(self, goals: Goals) -> tuple[Message, ...]:
        return tuple(directive.original(goals) for directive in self.directives)

    def overdue_directives(self, goal: Goal, goals: Goals, owner: ThreadIncarnation,
                           now: float) -> tuple[RequestDirective, ...]:
        return tuple(directive for directive in self.directives
                     if directive.overdue(goals, owner, now))

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
class GoalAuthority:
    """What an owner input was granted: this goal in this state.

    Assigning or replacing a problem order revises the goal but keeps its authority;
    pausing, completing, blocking, clearing or replacing the goal changes it.
    """

    goal_id: str
    state: GoalState


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
    def describe_execution(self, wait: GoalWait, snapshot: RegistrySnapshot,
                           agents: AgentActivity) -> ThreadGoalExecution: ...

    @abstractmethod
    def peer_change_values(self) -> GoalPeerChange: ...

    @abstractmethod
    def failed_turn_projection(self, path: Path, owner: Thread, owner_status: ThreadStatus,
                               claim_scope: int) -> FailedTurnProjection: ...

    @abstractmethod
    def stopped_permit(self, store: GoalAttemptStore) -> GoalTurnPermit: ...

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

    def block_after_failed_turn(self, goals: Goals, name: str, worktree: str,
                                diagnostic: str) -> GoalDeclaration:
        return self

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


class ReportedTurnState(DeclaredFamily, JsonShapeFamily, affix="ReportedTurnState"):
    """The reporting turn is a state; null/text remain the existing wire forms."""

    @abstractmethod
    def reports(self, turn_id: str) -> bool: ...


@dataclass(frozen=True)
class NoReportedTurn(ReportedTurnState, JsonShapeMember):
    value: None = None

    def reports(self, turn_id: str) -> bool:
        return False


@dataclass(frozen=True)
class ReportedTurn(ReportedTurnState, JsonShapeMember):
    value: str

    def reports(self, turn_id: str) -> bool:
        return self.value == turn_id


@dataclass(frozen=True)
class Goal(RecordMember, GoalDeclaration):
    """A goal owns one typed record for storage and agent-comms protocol boundaries."""

    text: str
    id: str
    published_seq: int
    progress: str = ""
    revision: int = 0
    reported_turn: ReportedTurnState = field(default_factory=NoReportedTurn)
    mention_source: GoalMentionSource | None = None
    state: GoalState = field(default_factory=ActiveGoal)
    order: GoalOrder = field(default_factory=UnorderedGoalOrder)

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

    def describe_execution(self, wait: GoalWait, snapshot: RegistrySnapshot,
                           agents: AgentActivity) -> ThreadGoalExecution:
        return wait.project(self, snapshot, agents)

    def peer_change_values(self) -> GoalPeerChange:
        return GoalPeerChange(goal=self.revision_key, state=type(self.state), progress=self.progress)

    def failed_turn_projection(self, path: Path, owner: Thread, owner_status: ThreadStatus,
                               claim_scope: int) -> FailedTurnProjection:
        return FailedTurnProjection.read_for_goal(path, owner, owner_status, claim_scope, self)

    def check_expected(self, current: GoalDeclaration) -> None:
        if self != current:
            raise ValueError("Goal changed during resume; refresh its state.")

    def accepts_following_handoff(self) -> bool:
        return self.state.active

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
        return self.reported_turn.reports(turn_id)

    @property
    def captured(self) -> GoalCheckpoint:
        return PresentGoalCheckpoint(self.revision_key)

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

    @property
    def revision_key(self) -> GoalRevision:
        return GoalRevision(self.id, self.revision)

    def accepts_observation(self, goal_id: str, minimum_revision: int) -> bool:
        """Later revisions may describe the same failed goal, never a replacement."""
        return self.id == goal_id and self.revision >= minimum_revision

    @property
    def summary(self) -> str:
        return f"Goal · {self.state.declared_name}: {self.text}"
