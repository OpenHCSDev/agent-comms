"""What a goal shows: current, absent or unavailable, as one declared family."""

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from agent_comms.declared_family import DeclaredFamily
from agent_comms.goals import Goal
from agent_comms.goal_presentation import GoalExecution, GoalExecutionState


class GoalDisplay(DeclaredFamily):
    can_control: ClassVar[bool] = True

    @property
    @abstractmethod
    def snapshot(self) -> Goal | None: ...

    @property
    def visible(self) -> bool:
        return self.snapshot is not None

    @abstractmethod
    def heading(self, execution: GoalExecution | None) -> str: ...

    def details_heading(self, execution: GoalExecution | None) -> str:
        return self.heading(execution)

    def standby(self, execution: GoalExecution | None) -> bool:
        return False

    def action_failure(self, error: Exception) -> str:
        return str(error)

    @staticmethod
    def current(goal: Goal | None) -> "GoalDisplay":
        return NoGoal() if goal is None else ShowingGoal(goal)


@dataclass(frozen=True)
class NoGoal(GoalDisplay):
    can_control: ClassVar[bool] = False
    @property
    def snapshot(self) -> None:
        return None

    def heading(self, execution: GoalExecution | None) -> str:
        return "No current goal"

    def action_failure(self, error: Exception) -> str:
        return "This goal has been cleared."


@dataclass(frozen=True)
class ShowingGoal(GoalDisplay):
    goal: Goal

    @property
    def snapshot(self) -> Goal:
        return self.goal

    def standby(self, execution: GoalExecution | None) -> bool:
        return (self.goal.state.active and execution is not None
                and execution.goal_id == self.goal.id
                and execution.state is GoalExecutionState.STANDBY)

    def heading(self, execution: GoalExecution | None) -> str:
        status = "Standby" if self.standby(execution) else self.goal.state.declared_name
        return f"Goal · {status} · rev {self.goal.revision}"

    def details_heading(self, execution: GoalExecution | None) -> str:
        heading = self.heading(execution)
        if self.standby(execution):
            heading += f" · {execution.presentation('').summary}"
        return heading


@dataclass(frozen=True)
class GoalUnavailable(GoalDisplay):
    last_confirmed: Goal | None
    can_control: ClassVar[bool] = False

    @property
    def snapshot(self) -> Goal | None:
        return self.last_confirmed

    @property
    def visible(self) -> bool:
        return True

    def heading(self, execution: GoalExecution | None) -> str:
        detail = "last confirmed snapshot" if self.last_confirmed else "reconnecting to owner"
        return f"Goal state unavailable · {detail}"
