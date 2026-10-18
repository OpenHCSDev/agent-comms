"""What a goal shows: current, absent or unavailable, as one declared family.

The owner's goal snapshot reaches the UI already decoded, with the turn
settlement the owner read alongside it.
"""

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from agent_comms.acp_extension import AgentCommsUpdate, decode_updates
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import AbsentGoal, Goal, ThreadGoal
from agent_comms.goal_presentation import ThreadGoalExecution, GoalSnapshot


@dataclass(frozen=True)
class OwnerGoalSnapshot(GoalSnapshot):
    """The live owner's answer to a goal read: the snapshot and the turn it read with it."""

    updates: tuple[AgentCommsUpdate, ...]

    @classmethod
    def from_owner(cls, result: Mapping) -> "OwnerGoalSnapshot":
        """Decode the owner's ``goal_snapshot`` reply once, at the socket boundary."""
        raw_goal, raw_execution = result["goal"], result["goalExecution"]
        return cls(
            FieldCodec.decode(ThreadGoal, raw_goal),
            FieldCodec.decode(ThreadGoalExecution, raw_execution),
            decode_updates(result["_meta"]),
        )


class GoalDisplay(DeclaredFamily):
    can_control: ClassVar[bool] = True

    @property
    @abstractmethod
    def snapshot(self) -> ThreadGoal: ...

    @property
    def visible(self) -> bool:
        return self.snapshot.fold(lambda goal: True, lambda: False)

    @abstractmethod
    def heading(self, execution: ThreadGoalExecution) -> str: ...

    def details_heading(self, execution: ThreadGoalExecution) -> str:
        return self.heading(execution)

    def standby(self, execution: ThreadGoalExecution) -> bool:
        return False

    def action_failure(self, error: Exception) -> str:
        return str(error)

    @staticmethod
    def current(goal: ThreadGoal) -> "GoalDisplay":
        return goal.fold(ShowingGoal, NoGoal)


@dataclass(frozen=True)
class NoGoal(GoalDisplay):
    can_control: ClassVar[bool] = False
    @property
    def snapshot(self) -> AbsentGoal:
        return AbsentGoal()

    def heading(self, execution: ThreadGoalExecution) -> str:
        return "No current goal"

    def action_failure(self, error: Exception) -> str:
        return "This goal has been cleared."


@dataclass(frozen=True)
class ShowingGoal(GoalDisplay):
    goal: Goal

    @property
    def snapshot(self) -> Goal:
        return self.goal

    def standby(self, execution: ThreadGoalExecution) -> bool:
        return execution.waiting_for(self.goal)

    def heading(self, execution: ThreadGoalExecution) -> str:
        status = "Standby" if self.standby(execution) else self.goal.state.declared_name
        return f"Goal · {status} · rev {self.goal.revision}"

    def details_heading(self, execution: ThreadGoalExecution) -> str:
        heading = self.heading(execution)
        if self.standby(execution):
            heading += f" · {execution.presentation('').summary}"
        return heading


@dataclass(frozen=True)
class GoalUnavailable(GoalDisplay):
    last_confirmed: ThreadGoal
    can_control: ClassVar[bool] = False

    @property
    def snapshot(self) -> ThreadGoal:
        return self.last_confirmed

    @property
    def visible(self) -> bool:
        return True

    def heading(self, execution: ThreadGoalExecution) -> str:
        # A failed read says nothing about why; the session's own state explains it.
        return self.last_confirmed.fold(
            lambda goal: "Goal state unavailable · last confirmed snapshot",
            lambda: "Goal state unavailable",
        )
