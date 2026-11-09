"""Declared values carried in a selected wake's work context."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Annotated

from agent_comms.nominal.field_codec import NullMember, RecordMember, SubclassUnion, WireName
from agent_comms.planning.goal_execution import GoalExecution
from agent_comms.registry.thread_text import ThreadText


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


class WakeWorkSource[Owner](ABC):
    @abstractmethod
    def wake_context(self, owner: Owner) -> WakeWorkContext: ...
