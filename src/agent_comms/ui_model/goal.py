"""Display contracts shared by goal declarations and execution observations."""

from abc import ABC, abstractmethod


class GoalExecutionDisplay[Subject](ABC):
    @abstractmethod
    def heading(self, revision: int) -> str: ...

    @abstractmethod
    def details_heading(self, heading: str) -> str: ...

    @abstractmethod
    def waiting_for(self, goal: Subject) -> bool: ...


class GoalDisplay[Snapshot, Subject](ABC):
    @property
    @abstractmethod
    def can_control(self) -> bool: ...

    @property
    @abstractmethod
    def snapshot(self) -> Snapshot: ...

    @property
    @abstractmethod
    def visible(self) -> bool: ...

    @abstractmethod
    def heading(self, execution: GoalExecutionDisplay[Subject]) -> str: ...

    def details_heading(self, execution: GoalExecutionDisplay[Subject]) -> str:
        return self.heading(execution)

    @abstractmethod
    def standby(self, execution: GoalExecutionDisplay[Subject]) -> bool: ...
