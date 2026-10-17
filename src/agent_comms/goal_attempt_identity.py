"""Goal identity without a reservation token or execution authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .field_codec import FieldCodec
from .thread_identity import RecordedThreadIncarnation

if TYPE_CHECKING:
    from .goals import Goal


class FailureNotObserved(ValueError):
    """This lifecycle or record does not carry a bound passive failure."""


@dataclass(frozen=True, slots=True)
class GoalAttemptIdentity:
    goal_id: str
    generation: int
    attempt_id: str


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
