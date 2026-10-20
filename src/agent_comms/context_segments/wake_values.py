"""Declared values carried in a selected wake's work context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self, cast

from ..field_codec import NullMember, RecordMember, SubclassUnion, compile_decoder
from ..goal_states import GoalState

if TYPE_CHECKING:
    from ..goal_management import Goals
    from ..threads import Thread


class NoWakeText(NullMember):
    """A title or original assignment that was not supplied."""


WakeText = str | NoWakeText


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
    title: WakeText
    tags: tuple[str, ...]
    original_assignment: WakeText
    current_goal: WakeGoalValue

    @classmethod
    def from_thread(cls, thread: Thread, goals: Goals) -> Self:
        text = compile_decoder(WakeText)
        return cls(
            name=thread.name,
            title=cast(WakeText, text.decode(thread.title)),
            tags=tuple(sorted(thread.tags)),
            original_assignment=cast(WakeText, text.decode(thread.task)),
            current_goal=thread.goal.wake_context(goals, thread.incarnation),
        )
