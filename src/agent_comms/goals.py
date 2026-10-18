"""Goals: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from abc import abstractmethod
import re

from .child_process import ExitedOutcome

from .declared_family import DeclaredFamily
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation

from .errors import RelationViolationError
from .field_codec import FieldCodec, MalformedRecord, NullMember, SubclassUnion
from .goal_states import ActiveGoal, GoalState

if TYPE_CHECKING:
    from .goal_management import Goals
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
        from .text_digest import TextDigest

        GoalRevision(self.goal_id, self.text_revision)
        TextDigest(self.text_digest)
        RecordedThreadIncarnation(self.owner_name, self.owner_created_at)

    @property
    def checkpoint(self) -> GoalRevision:
        return GoalRevision(self.goal_id, self.text_revision)

    @property
    def digest(self):
        from .text_digest import TextDigest

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

    @staticmethod
    def printed(stdout: bytes) -> int | None:
        """The count a check printed (its last integer), or None when it printed no integer."""
        counts = re.findall(r"\d+", stdout.decode(errors="replace"))
        return int(counts[-1]) if counts else None

    def require(self, result):
        if not isinstance(result.outcome, ExitedOutcome) or self.printed(result.stdout) != self.expected:
            raise ValueError(f"The order check did not print its expected last integer {self.expected}.")


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

    def serving(
        self, goal: Goal, goals: Goals, owner: ThreadIncarnation
    ) -> tuple[Todo, ...] | None:
        """The undelivered orders serving this Goal as an owner request; None if it is none."""
        return None

    def instructions(self, todos) -> str:
        return ""

    def todos(self, goals, owner, goal_id):
        return ()

    @abstractmethod
    def require_order(self, goal, goals, owner) -> None: ...

    @abstractmethod
    def require_replacement(self, goal, goals, owner) -> None: ...


@dataclass(frozen=True)
class UnorderedGoalOrder(GoalOrder):
    def require_order(self, goal, goals, owner) -> None:
        pass

    def require_replacement(self, goal, goals, owner) -> None:
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


@dataclass(frozen=True)
class ProblemGoalOrder(GoalOrder):
    """A unit's one Goal groups its canonical Todo assignments, not copied orders."""

    StrikeLimit: ClassVar[int] = 2  # live strikes the commanded unit may take on one problem

    def require_completion(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        raise ValueError("Complete each problem with its checked delivered commit.")

    def require_order(self, goal, goals, owner):
        if not goals.todos.for_goal(owner, goal.id):
            raise ValueError("The problem Goal has no assigned Todos.")

    def require_replacement(self, goal, goals, owner):
        if goals.todos.holds_pending(owner):
            raise ValueError("Finish or release the assigned problems before replacing this Goal.")

    def todos(self, goals, owner, goal_id):
        return goals.todos.for_goal(owner, goal_id)

    def instructions(self, todos):
        return "\n\n".join(todo.order.instructions(todo) for todo in todos if todo.pending)


class ThreadGoal(SubclassUnion):
    """What a thread's goal slot holds: its Goal, or AbsentGoal when none is set or active.

    Each member answers what a thread projects from its goal, so no reader tests for absence.
    """

    @property
    @abstractmethod
    def active(self) -> ThreadGoal: ...

    @abstractmethod
    def identifies(self, goal_id: str) -> bool: ...

    def matching(self, goal_id: str) -> ThreadGoal:
        """This record when it is the named goal, at any later state or revision."""
        return self if self.identifies(goal_id) else AbsentGoal()

    @abstractmethod
    def continuing(self, captured: ThreadGoal) -> ThreadGoal:
        """This goal when it is still the goal captured earlier."""

    @abstractmethod
    def owes_project_resume(self, due: bool) -> bool:
        """Whether an idle thread still owes its resume turn; a goal turn is that turn."""

    @abstractmethod
    def task_scope(self, project: str): ...

    @abstractmethod
    def context_segments(self, thread, goals) -> tuple: ...

    @abstractmethod
    def wake_view(self) -> dict[str, object] | None: ...

    @abstractmethod
    def task_facts(self) -> tuple: ...

    @property
    @abstractmethod
    def goal_checkpoint(self) -> GoalCheckpoint: ...

    @abstractmethod
    def require_replacement(self, goals, owner) -> None: ...

    @abstractmethod
    def require_set(self) -> Goal: ...

    @abstractmethod
    def require_current(self) -> Goal: ...

    @abstractmethod
    def accepts_observation(self, goal_id: str, minimum_revision: int) -> bool: ...


@dataclass(frozen=True)
class AbsentGoal(NullMember, ThreadGoal):
    """No goal is set, or the set goal is not active; stored as null."""

    @property
    def active(self) -> AbsentGoal:
        return self

    def identifies(self, goal_id: str) -> bool:
        return False

    def continuing(self, captured: ThreadGoal) -> AbsentGoal:
        return self

    def owes_project_resume(self, due: bool) -> bool:
        return due

    def task_scope(self, project: str):
        from .task_sources import TurnTaskScope

        return TurnTaskScope(project=project)

    def context_segments(self, thread, goals) -> tuple:
        return ()

    def wake_view(self) -> None:
        return None

    def task_facts(self) -> tuple:
        return ()

    @property
    def goal_checkpoint(self) -> AbsentGoalCheckpoint:
        return AbsentGoalCheckpoint()

    def require_replacement(self, goals, owner) -> None:
        pass

    def require_set(self) -> Goal:
        raise ValueError("No goal is set for this thread.")

    def require_current(self) -> Goal:
        raise RelationViolationError("This goal was replaced or cleared; refresh its state.")

    def accepts_observation(self, goal_id: str, minimum_revision: int) -> bool:
        return False


@dataclass(frozen=True)
class Goal(ThreadGoal):
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

    def instructions(self, todos) -> str:
        return self.render_instructions(self.order.instructions(todos))

    def render_instructions(self, orders_text: str) -> str:
        return "\n\n".join(part for part in (self.text, orders_text) if part)

    def require_order(self, goals, owner) -> None:
        self.order.require_order(self, goals, owner)

    def require_replacement(self, goals, owner) -> None:
        self.order.require_replacement(self, goals, owner)

    @property
    def active(self) -> ThreadGoal:
        return self if self.state.active else AbsentGoal()

    def identifies(self, goal_id: str) -> bool:
        return self.id == goal_id

    def continuing(self, captured: ThreadGoal) -> ThreadGoal:
        return self if captured.identifies(self.id) else AbsentGoal()

    def owes_project_resume(self, due: bool) -> bool:
        return False

    def task_scope(self, project: str):
        from .task_sources import GoalTaskScope

        return GoalTaskScope(project=project, goal=self.checkpoint)

    def context_segments(self, thread, goals) -> tuple:
        from .turn_context import GoalSegment

        return (GoalSegment.capture(thread, self, goals),)

    def wake_view(self) -> dict[str, object]:
        return {"text": self.text, "status": self.state.declared_name, "progress": self.progress}

    def task_facts(self) -> tuple:
        from .retained_task_facts import GoalTaskFact

        return (GoalTaskFact(self),)

    @property
    def goal_checkpoint(self) -> PresentGoalCheckpoint:
        return PresentGoalCheckpoint(self.checkpoint)

    def require_set(self) -> Goal:
        return self

    def require_current(self) -> Goal:
        return self

    def after_failed_turn(self, diagnostic: str) -> Goal:
        progress = f"{self.progress}\n\n{diagnostic}" if self.progress else diagnostic
        return self._failure_projection(self.state.after_failed_turn(diagnostic), progress)

    def after_unverified_completion(self, diagnostic: str) -> Goal:
        return self._failure_projection(
            self.state.after_unverified_completion(diagnostic), diagnostic
        )

    def _failure_projection(self, state: GoalState, progress: str) -> Goal:
        if state is self.state:
            return self
        return replace(self, state=state, progress=progress, revision=self.revision + 1)

    @classmethod
    def from_wire(cls, data) -> Goal:
        return FieldCodec.decode_record(cls, data)

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.record_schema(cls)

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode_record(self)

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
