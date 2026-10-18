"""Behavior for the runnable/standby refinement of a goal's domain state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .field_codec import FieldCodec, WireValue
from .goal_states import (
    ActiveGoal,
    BlockedGoal,
    CompletedGoal,
    GoalState,
    PausedGoal,
    UnrecordedBlockGoal,
)
from .goals import Goal
from .thread_identity import RecordedThreadIncarnation
from .thread_presentation import ThreadPresentation

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery


class ExecutionPresentation(ABC):
    waiting = False

    @abstractmethod
    def render(self, execution: GoalExecution) -> tuple[str, str]: ...

    def represents(self, state: GoalState) -> bool:
        return False

    def waiting_presentation(self, execution: GoalExecution, ordinary: ThreadPresentation) -> ThreadPresentation:
        return ordinary


@dataclass(frozen=True)
class StateExecutionPresentation(ExecutionPresentation):
    state: type[GoalState]

    def represents(self, state: GoalState) -> bool:
        return state.__class__ is self.state

    def render(self, execution: GoalExecution) -> tuple[str, str]:
        return FieldCodec.decode(
            GoalState, self.state.wire_payload(execution.block_reason, None)
        ).presentation()


class StandbyExecutionPresentation(ExecutionPresentation):
    waiting = True

    def waiting_presentation(self, execution: GoalExecution, ordinary: ThreadPresentation) -> ThreadPresentation:
        return execution.presentation(ordinary.title)

    def render(self, execution: GoalExecution) -> tuple[str, str]:
        names = ", ".join(f"@{target.incarnation.name}" for target in execution.wait_for)
        idle = ", ".join(f"@{target.incarnation.name}" for target in execution.inactive_wait_for)
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

    RUNNABLE = ("runnable", StateExecutionPresentation(ActiveGoal))
    STANDBY = ("standby", StandbyExecutionPresentation())
    PAUSED = (FieldCodec.encode(PausedGoal), StateExecutionPresentation(PausedGoal))
    BLOCKED = (FieldCodec.encode(BlockedGoal), StateExecutionPresentation(BlockedGoal))
    UNRECORDED_BLOCK = (
        FieldCodec.encode(UnrecordedBlockGoal),
        StateExecutionPresentation(UnrecordedBlockGoal),
    )
    COMPLETED = (FieldCodec.encode(CompletedGoal), StateExecutionPresentation(CompletedGoal))

    @classmethod
    def for_domain_state(cls, state: GoalState) -> GoalExecutionState:
        return next(member for member in cls if member.view.represents(state))


@dataclass(frozen=True, slots=True)
class GoalWaitTarget(WireValue):
    incarnation: RecordedThreadIncarnation

    def sent(self, original: CommittedDelivery) -> bool:
        """Only this declared incarnation can supply a dependency reply."""
        return original.audience.sender_lookup == self.incarnation.lookup

    def to_wire(self) -> dict[str, str | float]:
        return self.incarnation.to_wire()

    @classmethod
    def from_wire(cls, data: Mapping) -> GoalWaitTarget:
        return cls(RecordedThreadIncarnation.from_wire(data))

    @classmethod
    def schema(cls) -> dict:
        return RecordedThreadIncarnation.schema()


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

    def waiting_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        return self.state.view.waiting_presentation(self, ordinary)

    def standby(self) -> bool:
        return self.state.view.waiting

    def waiting_for(self, goal: Goal) -> bool:
        return goal.state.active and self.goal_id == goal.id and self.standby()

    def validate_goal(self, goal: Goal | None) -> None:
        if goal is None or self.goal_id != goal.id:
            raise ValueError("Goal execution identity does not match its declaration")

    def to_wire(self) -> dict:
        """Use the one field codec; target births keep their original wire keys."""
        return FieldCodec.encode(self)

    @classmethod
    def from_wire(cls, data: Mapping) -> GoalExecution:
        return FieldCodec.decode(cls, data)


@dataclass(frozen=True, slots=True)
class IdleGoalExecution(WireValue):
    """No goal execution has been projected."""

    def to_wire(self) -> None:
        return None

    @classmethod
    def from_wire(cls, data) -> IdleGoalExecution:
        if data is not None:
            raise ValueError("An idle goal execution is null on the wire")
        return cls()

    @classmethod
    def schema(cls) -> dict:
        return {"type": "null"}

    def waiting_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        return ordinary

    def standby(self) -> bool:
        return False

    def waiting_for(self, goal: Goal) -> bool:
        return False

    def validate_goal(self, goal: Goal | None) -> None:
        pass


@dataclass(frozen=True)
class GoalSnapshot:
    """A thread's current goal and its scheduling projection, read together.

    The execution, when present, schedules exactly this goal.
    """

    goal: Goal | None
    execution: GoalExecution | IdleGoalExecution

    def __post_init__(self) -> None:
        self.execution.validate_goal(self.goal)
