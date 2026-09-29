"""Goals: declaration and persistence owners."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .field_codec import FieldCodec
from .goal_states import ActiveGoal, GoalState


@dataclass(frozen=True, slots=True)
class GoalMentionBinding:
    """An exact goal token resolved once, never rebound by a later name reuse."""

    token: str
    resolution: str
    peer_name: str | None = None
    peer_created_at: float | None = None

    def __post_init__(self) -> None:
        if not self.token or self.resolution not in {
            "resolved",
            "self",
            "unknown",
            "alias",
            "malformed",
            "limit_exceeded",
            "non_executable",
        }:
            raise ValueError("Invalid goal mention binding.")
        if self.resolution == "resolved":
            if (
                not self.peer_name
                or not isinstance(self.peer_created_at, (float, int))
                or isinstance(self.peer_created_at, bool)
                or not math.isfinite(self.peer_created_at)
            ):
                raise ValueError("Resolved goal mention requires a stable peer incarnation.")
        elif self.peer_name is not None or self.peer_created_at is not None:
            raise ValueError("Unresolved goal mention cannot name a peer incarnation.")


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
        if (
            not self.goal_id
            or type(self.text_revision) is not int
            or not 0 <= self.text_revision < 1 << 63
            or type(self.text_digest) is not str
            or len(self.text_digest) != 64
            or not self.owner_name
            or type(self.owner_created_at) not in {float, int}
            or not math.isfinite(self.owner_created_at)
        ):
            raise ValueError("Invalid goal mention source.")


@dataclass(frozen=True)
class GoalRevision:
    """A particular revision, independent of text and execution state."""

    id: str
    revision: int

    def __post_init__(self) -> None:
        if not self.id or self.revision < 0:
            raise ValueError("Goal revision requires identity and nonnegative revision")


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

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode(self)

    def __post_init__(self) -> None:
        if not self.text.strip() or not self.id:
            raise ValueError("A goal requires text and an identity.")
        if type(self.revision) is not int or not 0 <= self.revision < 1 << 63:
            raise ValueError("Goal revision must be an exact nonnegative 63-bit integer.")
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
