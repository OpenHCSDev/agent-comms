"""Goal-turn persistence contracts, independent of the concrete ledger."""

from abc import ABC, abstractmethod

from agent_comms.execution.goal_turn_permit import GoalTurnPermit
from agent_comms.nominal.field_codec import GoalId


class GoalPermitSource(ABC):
    @abstractmethod
    def stopped_permit(self, goal_id: GoalId) -> GoalTurnPermit: ...


class GoalStoreSource[Store](ABC):
    @abstractmethod
    def open_goal_store(self) -> Store: ...
