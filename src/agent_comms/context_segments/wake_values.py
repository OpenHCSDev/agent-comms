"""Declared values carried in a selected wake's work context."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..field_codec import NullMember, RecordMember, SubclassUnion
from ..goal_states import GoalState


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
