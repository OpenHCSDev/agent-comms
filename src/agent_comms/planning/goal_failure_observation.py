"""Failure readings used by goal execution; stored rows live in goal_ledger."""


from abc import ABC, abstractmethod
from dataclasses import dataclass
from sqlite3 import Connection

from agent_comms.planning.goal_mutation import GoalFailureProjection


class GoalFailureEvidence[Failure](ABC):
    @abstractmethod
    def execution[Execution](self, source: GoalFailureProjection[Execution, Failure]) -> Execution: ...


@dataclass(frozen=True)
class NoFailedTurnEvidence[Failure](GoalFailureEvidence[Failure]):
    def execution[Execution](self, source: GoalFailureProjection[Execution, Failure]) -> Execution:
        return source.unresolved()


class GenerationFailureSource[Failure](ABC):
    __slots__ = ()

    @abstractmethod
    def failure_evidence(self, conn: Connection) -> GoalFailureEvidence[Failure]: ...
