"""Goals: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import isfinite
import time
from typing import Literal
from abc import abstractmethod

from .declared_family import DeclaredFamily
from .thread_identity import ThreadIncarnation

from .field_codec import FieldCodec
from .goal_states import ActiveGoal, GoalState
from .goal_progress import GoalProgress, TextGoalProgress


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


class GoalCommand(DeclaredFamily, affix="GoalCommand"):
    """Command facts live with the goal's original mention proof, not a second store."""

    def require_report(self, progress: GoalProgress | None) -> None:
        pass

    def require_text_edit(self, actor: type) -> None:
        pass

    def require_replacement(self) -> None:
        pass

    def reissued(self, commander: ThreadIncarnation, registry) -> IssuedGoalCommand:
        raise ValueError("An open self-directed goal cannot be commandeered; resolve it first.")


@dataclass(frozen=True)
class NoGoalCommand(GoalCommand):
    pass


@dataclass(frozen=True)
class IssuedGoalCommand(GoalCommand):
    commander: ThreadIncarnation
    revision: int
    issued_at: float

    def __post_init__(self) -> None:
        self.commander.require_recorded()
        if type(self.revision) is not int or not 1 <= self.revision < 1 << 63:
            raise ValueError("A command requires a positive exact revision.")
        if (type(self.issued_at) not in (int, float)
                or not isfinite(self.issued_at) or self.issued_at <= 0):
            raise ValueError("A command requires a positive finite server issue time.")

    @classmethod
    def issue(cls, commander: ThreadIncarnation, revision: int = 1) -> IssuedGoalCommand:
        return cls(commander, revision, time.time())

    def reissued(self, commander: ThreadIncarnation, registry) -> IssuedGoalCommand:
        if self.commander.resolved(registry) != commander:
            raise ValueError("Only this goal's original commander may revise its order.")
        return type(self).issue(self.commander, self.revision + 1)

    def require_report(self, progress: GoalProgress | None) -> None:
        if progress is None:
            raise ValueError("Commanded goals require a complete report: stupid, deleted, delivery, next.")
        progress.require_report()

    def require_text_edit(self, actor: type) -> None:
        from .goal_actions import OrderInvocable

        if actor is not OrderInvocable:
            raise ValueError("Revise commanded goal text through its original commander's order.")

    def require_replacement(self) -> None:
        raise ValueError("An open commanded goal cannot be cleared or replaced; resolve it first.")


@dataclass(frozen=True, slots=True)
class GoalMentionSource:
    """Text-revision and owner-incarnation proof saved with its registry Goal."""

    goal_id: str
    text_revision: int
    text_digest: str
    owner_name: str
    owner_created_at: float
    bindings: tuple[GoalMentionBinding, ...]
    command: GoalCommand = field(
        default_factory=NoGoalCommand, metadata={"wire_omit_default": True}
    )

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
    def overdue(self, now: float) -> bool: ...


@dataclass(frozen=True)
class NoDeadline(GoalDeadline):
    def overdue(self, now: float) -> bool:
        return False


@dataclass(frozen=True)
class Deadline(GoalDeadline):
    at: float

    def __post_init__(self) -> None:
        if type(self.at) not in (int, float) or not isfinite(self.at) or self.at <= 0:
            raise ValueError("A goal deadline requires a positive finite Unix timestamp.")

    def overdue(self, now: float) -> bool:
        return now > self.at


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

    @property
    def command(self) -> GoalCommand:
        return self.mention_source.command if self.mention_source is not None else NoGoalCommand()

    def require_mention_source(self) -> GoalMentionSource:
        if self.mention_source is None:
            raise ValueError("This goal has no original recipient/contact proof.")
        return self.mention_source

    def require_replacement(self) -> None:
        if not self.state.terminal:
            self.command.require_replacement()

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

    def accepts_observation(self, goal_id: str, minimum_revision: int) -> bool:
        """Later revisions may describe the same failed goal, never a replacement."""
        return self.id == goal_id and self.revision >= minimum_revision

    @property
    def summary(self) -> str:
        return f"Goal · {self.state.declared_name}: {self.text}"
