"""The assignment read surface consumed by a goal, without a dependency on its stores."""

from abc import ABC, abstractmethod

from agent_comms.nominal.field_codec import GoalId
from agent_comms.registry.thread_identity import ThreadIncarnation


class GoalObservation(ABC):
    __slots__ = ()

    @property
    @abstractmethod
    def captured(self) -> object: ...

    @abstractmethod
    def accepts_observation(self, goal_id: GoalId, minimum_revision: int) -> bool: ...


class GoalOwner(ABC):
    __slots__ = ()
    name: str
    goal: GoalObservation

    @property
    @abstractmethod
    def incarnation(self) -> ThreadIncarnation: ...

    @abstractmethod
    def require_goal_assignment(self, actor: ThreadIncarnation) -> None: ...


class GoalAssignment(ABC):
    __slots__ = ()

    @abstractmethod
    def goal_instructions(self) -> tuple[str, ...]: ...

    @abstractmethod
    def progress_line(self) -> str: ...


class GoalAssignments(ABC):
    __slots__ = ()

    @abstractmethod
    def for_goal(self, owner: ThreadIncarnation, goal_id: GoalId) -> tuple[GoalAssignment, ...]: ...

    @abstractmethod
    def closed_for_goal(self, owner: ThreadIncarnation, goal_id: GoalId) -> tuple[GoalAssignment, ...]: ...

    @abstractmethod
    def holds_pending(self, owner: ThreadIncarnation) -> bool: ...


class GoalWorkSource(ABC):
    @property
    @abstractmethod
    def todos(self) -> GoalAssignments: ...
