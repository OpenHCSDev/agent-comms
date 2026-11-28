"""Goal store and standby-review contracts consumed by GoalAction without store imports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from agent_comms.execution.input_key import InputKey
from agent_comms.execution.reported_turn import ReportedTurnState
from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.attempt_resolution import ComplianceDecision, RetryDecision
from agent_comms.planning.goal_work import GoalWorkSource
from agent_comms.planning.goals import Goal
from agent_comms.registry.registration_change import RegistrationChange
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.registry_store import RegistryEdit
from agent_comms.registry.threads import Thread


class PreparedStandbyReview(ABC):
    """Unlocked dependency-reply review ready to validate and publish a standby wait."""

    __slots__ = ()
    owner: Thread

    @abstractmethod
    def commit_standby(
        self,
        goal: Goal,
        thread: Thread,
        reported_turn: ReportedTurnState,
        wait_for: tuple[str, ...],
        reviewed_inputs: tuple[InputKey, ...],
    ) -> None: ...


class GoalActionStore(GoalWorkSource):
    """Store surface consumed by GoalAction without importing input dispositions or wait recovery."""

    __slots__ = ()

    @abstractmethod
    def require_thread(self, name: str) -> Thread: ...

    @abstractmethod
    def is_thread_running(self, name: str) -> bool: ...

    @abstractmethod
    def registry_snapshot(self) -> RegistrySnapshot: ...

    @abstractmethod
    def commit_registration(self, edit: RegistryEdit, change: RegistrationChange) -> None: ...

    @abstractmethod
    def bus_revision(self) -> int: ...

    @abstractmethod
    def clear_goal_wait(self, goal_id: GoalId) -> bool: ...

    @abstractmethod
    def prepare_standby_review(
        self, thread: Thread, goal_id: GoalId, wait_for: Sequence[str]
    ) -> PreparedStandbyReview: ...

    @abstractmethod
    def resume_goal_generation(self, goal_id: GoalId, executing: bool) -> None: ...

    @abstractmethod
    def comply_goal_generation(
        self, goal_id: GoalId, decision: ComplianceDecision, executing: bool
    ) -> None: ...

    @abstractmethod
    def retire_goal_generation(self, goal_id: GoalId) -> None: ...

    @abstractmethod
    def create_goal_generation(self, goal_id: GoalId) -> None: ...

    @abstractmethod
    def authorize_goal_retry(self, goal_id: GoalId, decision: RetryDecision) -> None: ...
