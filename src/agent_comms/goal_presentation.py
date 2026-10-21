"""Behavior for the runnable/standby refinement of a goal's domain state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from agent_comms.lifecycle_capability import Active

from .field_codec import FieldCodec, NullMember, RecordMember, SubclassUnion, WireValue
from .goal_states import (
    ActiveGoal,
    BlockedGoal,
    CompletedGoal,
    GoalState,
    PausedGoal,
    UnrecordedBlockGoal,
)
from .goals import (
    Goal,
    GoalCheckpoint,
    GoalDeclaration,
)
from .thread_identity import RecordedThreadIncarnation
from .thread_presentation import ThreadPresentation

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery


class ExecutionPresentation(ABC):
    waiting = False

    @abstractmethod
    def render(self, execution: GoalExecution) -> tuple[str, str]: ...

    def details_heading(self, execution: GoalExecution, heading: str) -> str:
        return heading

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

    def details_heading(self, execution: GoalExecution, heading: str) -> str:
        return f"{heading} · {self.render(execution)[1]}"

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


class ThreadGoalExecution(SubclassUnion):
    """The execution contract, including absence, consumed by thread views."""

    __slots__ = ()

    def details_heading(self, heading: str) -> str:
        return heading

    @abstractmethod
    def waiting_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation: ...

    @abstractmethod
    def standby(self) -> bool: ...

    @abstractmethod
    def waiting_for(self, goal: Goal) -> bool: ...

    @abstractmethod
    def validate_goal(self, goal: GoalCheckpoint) -> None: ...


@dataclass(frozen=True)
class GoalExecution(RecordMember, ThreadGoalExecution):
    state: GoalExecutionState
    goal_id: str
    wait_for: tuple[GoalWaitTarget, ...] = ()
    inactive_wait_for: tuple[GoalWaitTarget, ...] = ()
    block_reason: str | None = None

    def details_heading(self, heading: str) -> str:
        return self.state.view.details_heading(self, heading)

    def presentation(self, title: str) -> ThreadPresentation:
        glyph, summary = self.state.view.render(self)
        return ThreadPresentation(title, glyph, summary)

    def waiting_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        return self.state.view.waiting_presentation(self, ordinary)

    def standby(self) -> bool:
        return self.state.view.waiting

    def waiting_for(self, goal: Goal) -> bool:
        return any(self.goal_id == active.id and self.standby()
                   for active in goal.state.items_with(Active, goal))

    def validate_goal(self, goal: GoalCheckpoint) -> None:
        goal.require_identity(self.goal_id)


@dataclass(frozen=True)
class IdleGoalExecution(NullMember, ThreadGoalExecution):
    """No goal execution has been projected."""

    def waiting_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        return ordinary

    def standby(self) -> bool:
        return False

    def waiting_for(self, goal: Goal) -> bool:
        return False

    def validate_goal(self, goal: GoalCheckpoint) -> None:
        pass


class GoalPublicationSnapshot(ABC):
    """Last published goal projection, including the not-yet-published state."""

    @abstractmethod
    def changed_by(self, current: GoalSnapshot) -> bool: ...

    @abstractmethod
    def standby(self) -> bool: ...


class UnpublishedGoalSnapshot(NullMember, GoalPublicationSnapshot):
    def changed_by(self, current: GoalSnapshot) -> bool:
        return True

    def standby(self) -> bool:
        return False


@dataclass(frozen=True)
class GoalSnapshot(GoalPublicationSnapshot):
    """A thread's current goal and its scheduling projection, read together.

    The execution, when present, schedules exactly this goal.
    """

    goal: GoalDeclaration
    execution: ThreadGoalExecution

    def changed_by(self, current: GoalSnapshot) -> bool:
        # Domain identity ignores renames, but a published wait target includes
        # its current name. Compare the declared presentation fields as well.
        return asdict(self) != asdict(current)

    def standby(self) -> bool:
        return self.execution.standby()

    @classmethod
    def from_snapshot(cls, snapshot: GoalSnapshot, *extra: object) -> Self:
        """This record carrying ``snapshot``, followed by its own declared fields."""
        return cls(*(getattr(snapshot, item.name) for item in fields(GoalSnapshot)), *extra)

    def __post_init__(self) -> None:
        self.execution.validate_goal(self.goal.captured)
