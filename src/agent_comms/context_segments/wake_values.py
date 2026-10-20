"""Declared values carried in a selected wake's work context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

from ..field_codec import NullMember, RecordMember, SubclassUnion
from ..goal_states import GoalState

if TYPE_CHECKING:
    from ..goal_management import Goals
    from ..threads import Thread


class WakeGoalValue(SubclassUnion):
    """A woken thread's goal, including the declared absence of one."""


class NoWakeGoalContext(NullMember, WakeGoalValue):
    pass


@dataclass(frozen=True)
class WakeGoalContext(RecordMember, WakeGoalValue):
    text: str
    state: GoalState = field(metadata={"wire_name": "status"})
    progress: str


@dataclass(frozen=True)
class WakeWorkContext(RecordMember):
    name: str
    title: str | None
    tags: tuple[str, ...]
    original_assignment: str | None
    current_goal: WakeGoalValue

    @classmethod
    def from_thread(cls, thread: Thread, goals: Goals) -> Self:
        return cls(
            name=thread.name,
            title=thread.title,
            tags=tuple(sorted(thread.tags)),
            original_assignment=thread.task,
            current_goal=thread.goal.wake_context(goals, thread.incarnation),
        )
