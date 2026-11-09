"""The execution read from an owning goal snapshot, without its action controls."""

from abc import ABC

from agent_comms.planning.goal_execution import GoalExecution


class GoalExecutionSnapshot(ABC):
    __slots__ = ()

    execution: GoalExecution
