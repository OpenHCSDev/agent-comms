"""Declared values carried in a selected wake's work context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from ..nominal.field_codec import NullMember, RecordMember, SubclassUnion, WireName
from ..planning.goal_presentation import GoalExecution
from ..registry.thread_text import ThreadText


class WakeGoalValue(SubclassUnion):
    """A woken thread's goal, including the declared absence of one."""


class NoWakeGoalContext(NullMember, WakeGoalValue):
    pass


@dataclass(frozen=True)
class WakeGoalContext(RecordMember, WakeGoalValue):
    text: str
    execution: Annotated[GoalExecution, WireName("status")]
    progress: str


@dataclass(frozen=True)
class WakeWorkContext(RecordMember):
    name: str
    title: ThreadText
    tags: tuple[str, ...]
    original_assignment: ThreadText
    current_goal: WakeGoalValue
