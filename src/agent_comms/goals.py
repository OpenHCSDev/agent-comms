"""Goals: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, Self
from abc import abstractmethod

from .declared_family import DeclaredFamily
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation

from .field_codec import FieldCodec, JsonShapeFamily, JsonShapeMember
from .goal_states import ActiveGoal, GoalState
from .recorded_time import EventTime, UnrecordedEventTime


@dataclass(frozen=True)
class Delivery(DeclaredFamily):
    evidence: str

    @property
    def summary(self) -> str:
        return self.evidence

    def require_completion(self) -> None:
        raise ValueError("Completion requires works_in_app evidence and a commit SHA.")

    @property
    def reported_sha(self) -> str:
        raise ValueError("Completion requires works_in_app with a commit SHA.")


class Unfinished(Delivery):
    pass


@dataclass(frozen=True)
class WorksInApp(Delivery):
    sha: str

    def require_completion(self) -> None:
        pass

    @property
    def reported_sha(self) -> str:
        return self.sha


@dataclass(frozen=True)
class GoalReport:
    stupid: str
    deleted: str
    delivery: Delivery
    next: str

    def __post_init__(self):
        if not all(value.strip() for value in (
                self.stupid, self.deleted, self.delivery.evidence, self.next)):
            raise ValueError("Delivery and report text must be nonempty.")

    def __str__(self) -> str:
        return (f"Stupid: {self.stupid}\nDeleted: {self.deleted}\n"
                f"Delivery: {self.delivery.summary}\nNext: {self.next}")


class GoalProgress(DeclaredFamily, JsonShapeFamily, affix="GoalProgress"):
    @classmethod
    def capture(cls, value: GoalProgress | GoalReport | str) -> GoalProgress:
        return value if isinstance(value, cls) else cls.from_wire(FieldCodec.encode(value))

    def require_report(self) -> GoalReport:
        raise ValueError("A commanded goal requires stupid, deleted, delivery and next.")

    def noted(self, note: str) -> Self:
        return self  # Runtime notes must not replace the last structured report.

    def failed(self, diagnostic: str) -> GoalProgress:
        return self.noted(diagnostic)

    @property
    def block_reason(self) -> str:
        return str(self)

    def __bool__(self) -> bool:
        return bool(str(self))


@dataclass(frozen=True)
class TextGoalProgress(GoalProgress, JsonShapeMember):
    value: str = ""

    def __str__(self) -> str:
        return self.value

    def noted(self, note: str) -> TextGoalProgress:
        return type(self)(f"{self.value}\n\n{note}" if self.value else note)

    def failed(self, diagnostic: str) -> TextGoalProgress:
        return type(self)(diagnostic)


@dataclass(frozen=True)
class ReportedGoalProgress(GoalProgress, JsonShapeMember):
    value: GoalReport

    def require_report(self) -> GoalReport:
        return self.value

    def __str__(self) -> str:
        return str(self.value)

    @property
    def block_reason(self) -> str:
        return self.value.next


@dataclass(frozen=True, slots=True)
class GoalRef:
    owner: RecordedThreadIncarnation
    goal_id: str

    def __post_init__(self):
        self.owner.require_recorded()
        GoalRevision(self.goal_id, 0)

    def same_order(self, other: GoalRef) -> bool:
        return self.goal_id == other.goal_id and self.owner.same_publication_birth(other.owner)

    def current(self, snapshot) -> Goal | None:
        if not self.owner.current(snapshot):
            return None
        return snapshot.require(self.owner.name).goal_for(self.goal_id)

    def require_current(self, registry) -> Goal:
        goal = self.current(registry.snapshot())
        if goal is None:
            raise ValueError("Goal reference is no longer current.")
        return goal


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


@dataclass(frozen=True)
class GoalAuthor(DeclaredFamily, affix="GoalAuthor"):
    """Ordinary goals share behavior here; orders override their differences."""
    def require_report(self, progress) -> None:
        pass

    def require_completion(self, progress, *, goals, goal):
        return UnrecordedEventTime()

    def require_current_order(self, goals, goal):
        pass

    def require_order_source(self, problem_id: str) -> None:
        raise ValueError("This goal is not a problem-bound order.")

    def require_self_direction(self):
        pass

    def require_target(self, goals, target, goal_id):
        if target.goal is not None and not target.goal.state.terminal:
            target.goal.author.require_self_direction()

    def notification_targets(self, goals, *, new_order):
        return ()

    @property
    def instruction(self):
        return ""


class UncommandedGoalAuthor(GoalAuthor):
    """The ordinary author's wire case; all behavior belongs to GoalAuthor."""


@dataclass(frozen=True, slots=True)
class GoalMentionSource:
    """Text-revision and owner-incarnation proof saved with its registry Goal."""

    goal_id: str
    text_revision: int
    text_digest: str
    owner_name: str
    owner_created_at: float
    bindings: tuple[GoalMentionBinding, ...]
    author: GoalAuthor = field(kw_only=True)

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
class Goal:
    """A goal owns one typed record for storage and agent-comms protocol boundaries."""

    text: str
    id: str
    progress: GoalProgress = field(default_factory=TextGoalProgress)
    revision: int = 0
    reported_turn: str | None = None
    mention_source: GoalMentionSource | None = None
    state: GoalState = field(default_factory=ActiveGoal)
    deadline: EventTime = field(
        default_factory=UnrecordedEventTime, metadata={"wire_omit_default": True}
    )
    delivery_time: EventTime = field(
        default_factory=UnrecordedEventTime, metadata={"wire_omit_default": True}
    )

    @property
    def author(self) -> GoalAuthor:
        if self.mention_source is None:
            raise ValueError("This goal has no recorded author; it cannot be treated as self-directed.")
        return self.mention_source.author

    def notification_targets(
        self, goals, *, previous: Goal | None
    ) -> tuple[ThreadIncarnation, ...]:
        source = self.mention_source
        return () if source is None else source.author.notification_targets(
            goals, new_order=not self.same_order_as(previous))

    def same_order_as(self, other: Goal | None) -> bool:
        return other is not None and self.id == other.id

    def after_failed_turn(self, diagnostic: str) -> Goal:
        return self._failure_projection(
            self.state.after_failed_turn(diagnostic), self.progress.noted(diagnostic)
        )

    def after_unverified_completion(self, diagnostic: str) -> Goal:
        return self._failure_projection(
            self.state.after_unverified_completion(diagnostic), self.progress.failed(diagnostic)
        )

    def _failure_projection(self, state: GoalState, progress: GoalProgress) -> Goal:
        if state is self.state:
            return self
        return replace(self, state=state, progress=progress, revision=self.revision + 1)

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode(self)

    def __post_init__(self) -> None:
        object.__setattr__(self, "progress", GoalProgress.capture(self.progress))
        if not self.text.strip() or not self.id:
            raise ValueError("A goal requires text and an identity.")
        GoalRevision(self.id, self.revision)
        self.require_deadline(self.deadline)
        if self.reported_turn is not None and not isinstance(self.reported_turn, str):
            raise ValueError("Goal reported turn must be a string or null.")

    @property
    def checkpoint(self) -> GoalRevision:
        return GoalRevision(self.id, self.revision)

    @staticmethod
    def require_deadline(deadline: EventTime):
        if any(at <= 0 for at in deadline.timestamps):
            raise ValueError("A goal deadline requires a positive Unix timestamp.")

    @property
    def late(self) -> bool:
        return any(delivered > deadline for delivered in self.delivery_time.timestamps
                   for deadline in self.deadline.timestamps)

    @property
    def delivery_complete(self) -> bool:
        return self.state.terminal and bool(self.delivery_time.timestamps)

    def accepts_observation(self, goal_id: str, minimum_revision: int) -> bool:
        """Later revisions may describe the same failed goal, never a replacement."""
        return self.id == goal_id and self.revision >= minimum_revision

    @property
    def summary(self) -> str:
        return f"Goal · {self.state.declared_name}: {self.text}"
