"""Goals: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Literal

from .change_report import FileLine
from abc import abstractmethod

from .declared_family import DeclaredFamily
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation

from .field_codec import FieldCodec
from .goal_states import ActiveGoal, GoalState


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
        from .errors import RelationViolationError

        try:
            peer.role.require_executable()
        except RelationViolationError:
            return (), (diagnostic,)
        if peer.created_at == owner.created_at:
            return (), (diagnostic,)
        return (
            GoalDerivedContact(
                owner.name,
                owner.created_at,
                peer.name,
                peer.created_at,
                goal.id,
                source.text_revision,
            ),
        ), ()


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
            self.digest.matches(goal.instructions)
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
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Goal revision must be an exact nonnegative 63-bit integer."
            ) from error
        if not 0 <= revision < 1 << 63:
            raise ValueError("Goal revision must be an exact nonnegative 63-bit integer.")


class GoalCheckpoint(DeclaredFamily, affix="GoalCheckpoint"):
    """Captured goal identity, not a goal record or execution permission."""


@dataclass(frozen=True)
class AbsentGoalCheckpoint(GoalCheckpoint, declared_name="absent"):
    pass


@dataclass(frozen=True)
class PresentGoalCheckpoint(GoalCheckpoint, declared_name="present"):
    revision: GoalRevision


@dataclass(frozen=True)
class OrderCheck:
    directory: str
    command: str
    expected: Literal[0] = 0

    def __post_init__(self):
        if not self.directory.strip() or not self.command.strip() or self.expected != 0:
            raise ValueError("An order check requires a directory, a silent command and exit status zero.")

    def require_directory(self):
        if not Path(self.directory).is_dir():
            raise ValueError("The order check directory does not exist.")

class GoalOrder(DeclaredFamily, affix="GoalOrder"):
    def require_slot(self, goal, goal_id, problem):
        if goal.id == goal_id:
            raise ValueError("Goal ID belongs to another problem.")
        goal.state.require_order_slot()

    def require_completion(self) -> None:
        pass

    def installed(self, works: str) -> GoalOrder:
        raise ValueError("This goal is not a problem order.")

    def require_close(self) -> None:
        raise ValueError("Closing a problem requires recorded installed application evidence.")

    def delivery_check(self) -> OrderCheck:
        raise ValueError("This goal is not a problem order.")

    def instructions(self, text: str) -> str:
        return text

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
class ProblemGoalOrder(GoalOrder):
    problem: str
    owner: FileLine
    check: OrderCheck
    edits: tuple[str, ...]
    banned: tuple[str, ...]
    report: str

    def __post_init__(self):
        if not self.problem or not self.edits or not self.banned or not self.report.strip():
            raise ValueError("An order requires its problem, edits, banned actions and report.")
        if any(not text.strip() for text in (*self.edits, *self.banned)):
            raise ValueError("Order edits and banned actions cannot be empty.")

    def require_slot(self, goal, goal_id, problem):
        if (goal.id, self.problem) != (goal_id, problem):
            super().require_slot(goal, goal_id, problem)

    def delivery_check(self):
        return self.check

    def instructions(self, text):
        return "\n".join((text, f"Problem: {self.problem}", f"Owner: {self.owner.to_wire()}",
            f"Check: {self.check.directory} :: {self.check.command} -> {self.check.expected}",
            *(f"{n}. {edit}" for n, edit in enumerate(self.edits, 1)),
            "Banned: " + "; ".join(self.banned), "Report: " + self.report))

    def copy_as(self, kind, **evidence):
        return kind(**{f.name: getattr(self, f.name) for f in fields(ProblemGoalOrder)}, **evidence)

    def installed(self, works):
        return self.copy_as(InstalledProblemGoalOrder, works=works)

    def require_completion(self) -> None:
        raise ValueError("Complete a problem order with its checked delivered commit.")

    def require_order(self, goal, goals, owner) -> None:
        todo = goals.todos.get(self.problem)
        todo.require_assignee(owner, goal.id)
        todo.require_not_fired()

    def require_replacement(self, goal, goals, owner) -> None:
        goals.todos.get(self.problem).require_released_order(owner, goal.id)


@dataclass(frozen=True)
class InstalledProblemGoalOrder(ProblemGoalOrder):
    works: str

    def __post_init__(self):
        super().__post_init__()
        if len(self.works.split()) < 8:
            raise ValueError("Describe the installed application result in at least eight words.")

    def require_close(self):
        pass


@dataclass(frozen=True)
class Goal:
    """A goal owns one typed record for storage and agent-comms protocol boundaries."""

    text: str
    id: str
    progress: str = ""
    revision: int = 0
    reported_turn: str | None = None
    mention_source: GoalMentionSource | None = None
    state: GoalState = field(default_factory=ActiveGoal)
    order: GoalOrder = field(default_factory=UnorderedGoalOrder)

    @property
    def instructions(self) -> str:
        return self.order.instructions(self.text)

    def require_order(self, goals, owner) -> None:
        self.order.require_order(self, goals, owner)

    def require_replacement(self, goals, owner) -> None:
        self.order.require_replacement(self, goals, owner)

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

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode(self)

    def __post_init__(self) -> None:
        if not self.text.strip() or not self.id:
            raise ValueError("A goal requires text and an identity.")
        GoalRevision(self.id, self.revision)
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
