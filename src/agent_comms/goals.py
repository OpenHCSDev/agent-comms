"""Goals: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import isfinite
from pathlib import Path
import logging
from typing import Literal
from abc import abstractmethod

from .declared_family import DeclaredFamily
from .coordination_errors import PublicationUncertain
from .thread_identity import ThreadIncarnation

from .field_codec import FieldCodec
from .goal_states import ActiveGoal, GoalState
from .goal_progress import GoalProgress, TextGoalProgress
from .recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from .message_reference import MessageReference

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GoalRef:
    owner: ThreadIncarnation
    goal_id: str

    def __post_init__(self):
        self.owner.require_recorded()
        GoalRevision(self.goal_id, 0)

    def require_current(self, registry) -> Goal:
        thread = registry.require(self.owner.name)
        if thread.incarnation != self.owner:
            raise ValueError("Goal reference owner incarnation changed.")
        return thread.require_goal(self.goal_id)


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
        ThreadIncarnation(self.peer_name, self.peer_created_at)

    @property
    def peer(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.peer_name, self.peer_created_at)

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


class GoalAuthor(DeclaredFamily, affix="GoalAuthor"):
    @abstractmethod
    def require_report(self, progress) -> None: ...

    @abstractmethod
    def require_completion(self, progress, *, root: Path, goal: GoalRef) -> EventTime: ...

    @abstractmethod
    def require_current_order(self, root: Path, goal: GoalRef) -> None: ...

    def require_order_source(self, problem_id: str) -> None:
        raise ValueError("This goal is not a problem-bound order.")

    @abstractmethod
    def complete_problem(self, goals, reference: GoalRef) -> None: ...

    @abstractmethod
    def require_self_direction(self) -> None: ...

    @abstractmethod
    def revised(self, author: ThreadIncarnation, registry, now: float): ...

    @abstractmethod
    def has_report(self, recorded: EventTime) -> bool: ...

    @property
    @abstractmethod
    def revision_count(self) -> int: ...

    @abstractmethod
    def notification_targets(
        self, registry, root: Path, *, new_order: bool
    ) -> tuple[ThreadIncarnation, ...]: ...

    @property
    @abstractmethod
    def issued_times(self) -> tuple[float, ...]: ...

    @property
    @abstractmethod
    def instruction(self) -> str: ...


@dataclass(frozen=True)
class SelfDirectedGoalAuthor(GoalAuthor):
    def require_report(self, progress) -> None:
        pass

    def require_completion(self, progress, *, root, goal):
        return UnrecordedEventTime()

    def require_current_order(self, root, goal):
        pass

    def complete_problem(self, goals, reference):
        pass

    def require_self_direction(self):
        pass

    def revised(self, author, registry, now):
        raise ValueError("This existing goal was not issued by a commander.")

    def has_report(self, recorded):
        return recorded.since(0)

    @property
    def revision_count(self):
        return 0

    def notification_targets(self, registry, root, *, new_order):
        return ()

    @property
    def issued_times(self):
        return ()

    @property
    def instruction(self):
        return ""


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
        ThreadIncarnation(self.owner_name, self.owner_created_at)

    @property
    def checkpoint(self) -> GoalRevision:
        return GoalRevision(self.goal_id, self.text_revision)

    @property
    def digest(self):
        from .text_digest import TextDigest

        return TextDigest(self.text_digest)

    @property
    def incarnation(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.owner_name, self.owner_created_at)

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


class GoalDeadline(DeclaredFamily):
    @abstractmethod
    def overdue(self, now: float, delivery: EventTime) -> bool: ...

    @abstractmethod
    def late(self, delivery: EventTime) -> bool: ...


@dataclass(frozen=True)
class NoDeadline(GoalDeadline):
    def overdue(self, now: float, delivery: EventTime) -> bool:
        return False

    def late(self, delivery: EventTime) -> bool:
        return False


@dataclass(frozen=True)
class Deadline(GoalDeadline):
    at: float

    def __post_init__(self) -> None:
        if not isfinite(self.at) or self.at <= 0:
            raise ValueError("A goal deadline requires a positive finite Unix timestamp.")

    def overdue(self, now: float, delivery: EventTime) -> bool:
        return now > self.at and not delivery.timestamps

    def late(self, delivery: EventTime) -> bool:
        return any(timestamp > self.at for timestamp in delivery.timestamps)


class GoalNotice(DeclaredFamily, affix="GoalNotice"):
    @classmethod
    def publish(cls, bus, message, target: ThreadIncarnation) -> GoalNotice:
        try:
            # Core publication must not implicitly answer the unit's input.
            published = bus.publisher.publish_ordinary(message)
            return SentGoalNotice(published.reference)
        except PublicationUncertain as error:
            # Only the publisher's explicit uncertainty becomes this result;
            # programming errors and invalid requests must remain visible.
            _LOG.exception("Order saved; notification unconfirmed; no automatic resend")
            return UnconfirmedGoalNotice(target, f"{type(error).__name__}: {error}")

    @property
    @abstractmethod
    def summary(self) -> str: ...


@dataclass(frozen=True)
class SentGoalNotice(GoalNotice):
    message: MessageReference

    @property
    def summary(self):
        return "Commander notified"


@dataclass(frozen=True)
class UnconfirmedGoalNotice(GoalNotice):
    target: ThreadIncarnation
    reason: str

    @property
    def summary(self):
        return f"Commander notice unconfirmed: {self.reason}; no automatic resend"


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
    deadline: GoalDeadline = field(
        default_factory=NoDeadline, metadata={"wire_omit_default": True}
    )
    report_time: EventTime = field(
        default_factory=UnrecordedEventTime, metadata={"wire_omit_default": True}
    )
    work_time: EventTime = field(
        default_factory=UnrecordedEventTime, metadata={"wire_omit_default": True}
    )
    delivery_time: EventTime = field(
        default_factory=UnrecordedEventTime, metadata={"wire_omit_default": True}
    )
    notices: tuple[GoalNotice, ...] = field(default=(), metadata={"wire_omit_default": True})

    def with_text(self, text, owner, registry, *, author: GoalAuthor):
        from .goal_mentions import bind_goal_mentions

        text = text.strip()
        if not text:
            raise ValueError("A goal requires text.")
        revision = self.revision + 1
        return replace(self, text=text, revision=revision, mention_source=bind_goal_mentions(
            text, self.id, revision, owner, registry, author=author,
        ))

    @property
    def author(self) -> GoalAuthor:
        if self.mention_source is None:
            raise ValueError("This goal has no recorded author; it cannot be treated as self-directed.")
        return self.mention_source.author

    def notification_targets(
        self, registry, root: Path, *, previous: Goal | None
    ) -> tuple[ThreadIncarnation, ...]:
        source = self.mention_source
        return () if source is None else source.author.notification_targets(
            registry, root, new_order=not self.same_order_as(previous))

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
        if self.reported_turn is not None and not isinstance(self.reported_turn, str):
            raise ValueError("Goal reported turn must be a string or null.")

    @property
    def checkpoint(self) -> GoalRevision:
        return GoalRevision(self.id, self.revision)

    @property
    def late(self) -> bool:
        return self.deadline.late(self.delivery_time)

    @property
    def delivery_complete(self) -> bool:
        return self.state.terminal and bool(self.delivery_time.timestamps)

    def accepts_observation(self, goal_id: str, minimum_revision: int) -> bool:
        """Later revisions may describe the same failed goal, never a replacement."""
        return self.id == goal_id and self.revision >= minimum_revision

    @property
    def summary(self) -> str:
        return f"Goal · {self.state.declared_name}: {self.text}"
