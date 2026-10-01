"""Behavior for the runnable/standby refinement of a goal's domain state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .field_codec import FieldCodec
from .goal_states import (
    ActiveGoal,
    BlockedGoal,
    CompletedGoal,
    GoalState,
    PausedGoal,
    UnrecordedBlockGoal,
)
from .thread_presentation import ThreadPresentation

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery


class ExecutionPresentation(ABC):
    waiting = False

    @abstractmethod
    def render(self, execution: GoalExecution) -> tuple[str, str]: ...


@dataclass(frozen=True)
class StateExecutionPresentation(ExecutionPresentation):
    state: type[GoalState]

    def render(self, execution: GoalExecution) -> tuple[str, str]:
        return FieldCodec.decode(
            GoalState, self.state.wire_payload(execution.block_reason, None)
        ).presentation()


class StandbyExecutionPresentation(ExecutionPresentation):
    waiting = True

    def render(self, execution: GoalExecution) -> tuple[str, str]:
        names = ", ".join(f"@{target.name}" for target in execution.wait_for)
        idle = ", ".join(f"@{target.name}" for target in execution.inactive_wait_for)
        suffix = f"; no active turn: {idle}" if idle else ""
        return "◌", f"Standby · waiting for {names}{suffix}"


class GoalExecutionState(StrEnum):
    view: ExecutionPresentation

    def __new__(cls, value: str, view: ExecutionPresentation | None = None) -> GoalExecutionState:
        assert view is not None  # Enum declarations supply behavior; value lookup uses EnumMeta.
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.view = view
        return obj

    RUNNABLE = (ActiveGoal().execution_name, StateExecutionPresentation(ActiveGoal))
    STANDBY = ("standby", StandbyExecutionPresentation())
    PAUSED = (PausedGoal.declared_name, StateExecutionPresentation(PausedGoal))
    BLOCKED = (BlockedGoal.declared_name, StateExecutionPresentation(BlockedGoal))
    UNRECORDED_BLOCK = (
        UnrecordedBlockGoal.declared_name,
        StateExecutionPresentation(UnrecordedBlockGoal),
    )
    COMPLETED = (CompletedGoal.declared_name, StateExecutionPresentation(CompletedGoal))


@dataclass(frozen=True, slots=True)
class GoalWaitTarget:
    name: str
    created_at: float

    def sent(self, original: CommittedDelivery) -> bool:
        """Only this declared incarnation can supply a dependency reply."""
        from .bus_publication import stable_thread_lookup

        return original.audience.sender_lookup == stable_thread_lookup(self.created_at)


@dataclass(frozen=True, slots=True)
class GoalExecution:
    state: GoalExecutionState
    goal_id: str
    wait_for: tuple[GoalWaitTarget, ...] = ()
    inactive_wait_for: tuple[GoalWaitTarget, ...] = ()
    block_reason: str | None = None

    def presentation(self, title: str) -> ThreadPresentation:
        glyph, summary = self.state.view.render(self)
        return ThreadPresentation(title, glyph, summary)

    @classmethod
    def from_wire(cls, data: Mapping) -> GoalExecution:
        return cls(
            GoalExecutionState(data["state"]),
            str(data["goal_id"]),
            tuple(GoalWaitTarget(**target) for target in data.get("wait_for", ())),
            tuple(GoalWaitTarget(**target) for target in data.get("inactive_wait_for", ())),
            data.get("block_reason"),
        )
