"""The owning goal snapshot and original wait rows presentation reads."""

from abc import ABC, abstractmethod
from collections.abc import Callable

from agent_comms.planning.goal_execution_snapshot import GoalExecutionSnapshot
from agent_comms.planning.goal_waits import GoalWait, GoalWaits
from agent_comms.planning.goals import GoalDeclaration


class GoalSnapshots[Owner, Snapshot: GoalExecutionSnapshot, Registry](ABC):
    __slots__ = ()

    waits: GoalWaits

    @abstractmethod
    def snapshot_for(self, owner: Owner, snapshot: Registry,
                     wait: Callable[[GoalDeclaration], GoalWait]) -> Snapshot:
        ...
