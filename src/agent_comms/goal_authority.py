"""The authority a running turn captures, independent of the goal document's lifecycle."""

from __future__ import annotations

from dataclasses import dataclass

from .field_codec import GoalId, NullMember, RecordMember, SubclassUnion
from .goal_states import GoalState


class GoalTurnAuthority(SubclassUnion, affix=""):
    """Absent authority stays absent; a captured goal supplies the continuing-state rule."""

    def keeps_turn_under(self, current: GoalTurnAuthority) -> bool:
        return current == self

    def continues(self, captured: GoalAuthority) -> bool:
        return False


@dataclass(frozen=True)
class NoGoalAuthority(NullMember, GoalTurnAuthority):
    """The absent member, declared beside the captured member before any consumer compiles it."""


@dataclass(frozen=True)
class GoalAuthority(RecordMember, GoalTurnAuthority):
    """What an owner input was granted: this goal in this state.

    Assigning or replacing a problem order revises the goal but keeps its authority, and an
    order or retry that makes the same goal active keeps a running turn; pausing, completing,
    blocking, clearing or replacing the goal ends it.
    """

    goal_id: GoalId
    state: GoalState

    def keeps_turn_under(self, current: GoalTurnAuthority) -> bool:
        """Whether a turn granted this authority may keep working under the current goal."""
        return current.continues(self)

    def continues(self, captured: GoalAuthority) -> bool:
        return self.goal_id == captured.goal_id and self.state.continues_turn_from(captured.state)
