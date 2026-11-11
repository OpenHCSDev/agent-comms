from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from typing import ClassVar, Self

from agent_comms.nominal.field_codec import NullMember
from agent_comms.nominal.lifecycle_capability import Active
from agent_comms.planning.goal_actions import (
    ActiveGoalAction,
    GoalAction,
    PausedGoalAction,
    RetryGoalAction,
)
from agent_comms.planning.goal_execution import (
    BlockedExecution,
    BoundGoalExecution,
    GoalExecution,
    WaitingExecution,
)
from agent_comms.planning.goal_execution_snapshot import GoalExecutionSnapshot
from agent_comms.planning.goal_ledger import FailedTurnEvidence
from agent_comms.planning.goal_states import PauseSource
from agent_comms.planning.goal_wait_target import GoalWaitTarget
from agent_comms.planning.goals import Goal, GoalCheckpoint, GoalDeclaration


class GoalExecutionControls(GoalExecution, declared_name=None):
    """Control behavior for the observed execution, separate from its data contract."""

    toggle_label: ClassVar[str] = "Pause"

    def can_toggle(self) -> bool:
        return False

    async def apply_toggle(
        self, change: Callable[[type[GoalAction]], Awaitable[None]],
        unavailable: Callable[[], None],
    ) -> None:
        unavailable()


@dataclass(frozen=True)
class IdleGoalExecution(GoalExecutionControls):
    def validate_goal(self, goal: GoalCheckpoint) -> None:
        pass

    def heading(self, revision: int) -> str:
        return "No current goal"


class ControlledExecution(GoalExecutionControls, BoundGoalExecution, declared_name=None):
    """An execution the owner moves on with one control; the others offer none."""

    def can_toggle(self) -> bool:
        return True

    async def apply_toggle(
        self,
        change: Callable[[type[GoalAction]], Awaitable[None]],
        unavailable: Callable[[], None],
    ) -> None:
        await change(self.toggle)

    @property
    @abstractmethod
    def toggle(self) -> type[GoalAction]: ...


class ActiveExecution(ControlledExecution, declared_name=None):
    @property
    def toggle(self) -> type[GoalAction]:
        return PausedGoalAction


class RetryableExecution(ControlledExecution, declared_name=None):
    toggle_label = "Retry"

    @property
    def toggle(self) -> type[GoalAction]:
        return RetryGoalAction


@dataclass(frozen=True)
class RunnableExecution(ActiveExecution):
    label = "Active"

    def runnable(self) -> bool:
        return True


@dataclass(frozen=True)
class RunningExecution(ActiveExecution):
    label = "Running"


@dataclass(frozen=True)
class CompletedExecution(GoalExecutionControls, BoundGoalExecution):
    label = "Completed"


@dataclass(frozen=True)
class PausedExecution(WaitingExecution, ControlledExecution):
    source: PauseSource
    label = "Paused"
    toggle_label = "Resume"

    @property
    def toggle(self) -> type[GoalAction]:
        return ActiveGoalAction


@dataclass(frozen=True)
class StandbyExecution(ActiveExecution, WaitingExecution):
    wait_for: tuple[GoalWaitTarget, ...]
    inactive_wait_for: tuple[GoalWaitTarget, ...]
    label = "Standby"
    glyph = "◌"

    @property
    def summary(self) -> str:
        names = ", ".join(f"@{target.incarnation.name}" for target in self.wait_for)
        idle = ", ".join(f"@{target.incarnation.name}" for target in self.inactive_wait_for)
        suffix = f"; no active turn: {idle}" if idle else ""
        return f"Standby · waiting for {names}{suffix}"

    def details_heading(self, heading: str) -> str:
        return f"{heading} · {self.summary}"

    def standby(self) -> bool:
        return True

    def waiting_for(self, goal: Goal) -> bool:
        return goal.id == self.goal_id and bool(goal.state.items_with(Active, goal))


@dataclass(frozen=True)
class RetryableBlockExecution(BlockedExecution, RetryableExecution):
    block_reason: str

    @property
    def reason(self) -> str:
        return self.block_reason


@dataclass(frozen=True)
class AttemptUnresolvedExecution(GoalExecutionControls, BlockedExecution):
    @property
    def reason(self) -> str:
        return "inspect the unknown attempt; never replay"

    def unit_block_line(self, unit: str, orders: tuple[str, ...]) -> str:
        return (f"UNRESOLVED BLOCK {unit}: orders {', '.join(orders)}; "
                f"goal {self.goal_id} -> {self.reason}")


@dataclass(frozen=True)
class AttemptFailedExecution(BlockedExecution, RetryableExecution):
    evidence: FailedTurnEvidence

    @property
    def reason(self) -> str:
        return f"{self.evidence.failure.display_name()}: {self.evidence.failure_text}"


class GoalPublicationSnapshot(ABC):
    @abstractmethod
    def goal_changed(self, goal: GoalDeclaration) -> bool:
        """Whether an owner declaration requests a fresh projection, not its complete inputs."""

    @abstractmethod
    def changed_by(self, current: GoalSnapshot) -> bool: ...

    @abstractmethod
    def standby(self) -> bool: ...


class UnpublishedGoalSnapshot(NullMember, GoalPublicationSnapshot):
    def goal_changed(self, goal: GoalDeclaration) -> bool:
        return True

    def changed_by(self, current: GoalSnapshot) -> bool:
        return True

    def standby(self) -> bool:
        return False


@dataclass(frozen=True)
class GoalSnapshot(GoalPublicationSnapshot, GoalExecutionSnapshot):
    goal: GoalDeclaration
    execution: GoalExecutionControls

    def goal_changed(self, goal: GoalDeclaration) -> bool:
        return self.goal != goal

    def changed_by(self, current: GoalSnapshot) -> bool:
        return self != current

    def standby(self) -> bool:
        return self.execution.standby()

    @classmethod
    def from_snapshot(cls, snapshot: GoalSnapshot, *extra: object) -> Self:
        return cls(*(getattr(snapshot, item.name) for item in fields(GoalSnapshot)), *extra)

    def __post_init__(self) -> None:
        self.execution.validate_goal(self.goal.captured)
