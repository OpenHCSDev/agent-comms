from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from agent_comms.nominal.field_codec import GoalId, NullMember, RecordMember, SubclassUnion
from agent_comms.planning.goal_states import GoalState


class GoalTurnAuthority(SubclassUnion, affix=""):
    """The current goal decides whether an accepted input can keep working."""

    def keeps_turn_under(self, current: GoalTurnAuthority) -> bool:
        return current == self

    def continues(self, captured: GoalAuthority) -> bool:
        return False

    @abstractmethod
    def describe(self) -> str:
        """This authority in the words a refusal reads back to the model."""


@dataclass(frozen=True)
class NoGoalAuthority(NullMember, GoalTurnAuthority):
    """Clearing an assignment leaves an already accepted input free to continue."""

    def continues(self, captured: GoalAuthority) -> bool:
        return True

    def describe(self) -> str:
        return "no goal"


@dataclass(frozen=True)
class GoalAuthority(RecordMember, GoalTurnAuthority):
    """What an owner input was granted: this goal in this state.

    Assigning or replacing a problem order revises the goal but keeps its authority, and an
    order or retry that makes the same goal active keeps a running turn; pausing, completing,
    blocking or replacing the goal ends it. Clearing the goal removes that restriction.
    """

    goal_id: GoalId
    state: GoalState

    def keeps_turn_under(self, current: GoalTurnAuthority) -> bool:
        """Whether a turn granted this authority may keep working under the current goal."""
        return current.continues(self)

    def continues(self, captured: GoalAuthority) -> bool:
        return self.goal_id == captured.goal_id and self.state.continues_turn_from(captured.state)

    def describe(self) -> str:
        return f"goal {self.goal_id} in state {self.state.declared_name}"
