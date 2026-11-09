"""Execution observations carried by snapshots, wake values and wire records."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.goals import Goal, GoalCheckpoint
from agent_comms.registry.thread_presentation import ThreadPresentation
from agent_comms.ui_model.goal import GoalExecutionDisplay


class GoalExecution(DeclaredFamily, GoalExecutionDisplay[Goal], affix="Execution",
                    discovery_package='agent_comms.planning.goal_presentation'):
    """The complete execution family consumed by commands, views and the teammate."""

    def details_heading(self, heading: str) -> str:
        return heading

    def waiting_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        return ordinary

    def runnable(self) -> bool:
        return False

    def standby(self) -> bool:
        return False

    def waiting_for(self, goal: Goal) -> bool:
        return False

    def unit_blocks(self) -> tuple[BlockedExecution, ...]:
        return ()

    @abstractmethod
    def validate_goal(self, goal: GoalCheckpoint) -> None: ...

    @abstractmethod
    def heading(self, revision: int) -> str: ...


@dataclass(frozen=True)
class BoundGoalExecution(GoalExecution, declared_name=None):
    goal_id: GoalId
    glyph: ClassVar[str] = "✓"
    @property
    @abstractmethod
    def label(self) -> str: ...

    @property
    def summary(self) -> str:
        return self.label

    def presentation(self, title: str) -> ThreadPresentation:
        return ThreadPresentation(title, self.glyph, self.summary)

    def heading(self, revision: int) -> str:
        return f"Goal · {self.label} · rev {revision}"

    def validate_goal(self, goal: GoalCheckpoint) -> None:
        goal.require_identity(self.goal_id)


class WaitingExecution(BoundGoalExecution, declared_name=None):
    def waiting_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        return self.presentation(ordinary.title)


class BlockedExecution(WaitingExecution, declared_name=None):
    glyph = "!"
    label = "Blocked"

    @property
    @abstractmethod
    def reason(self) -> str: ...

    @property
    def summary(self) -> str:
        reason = " ".join(self.reason.split())
        summary = reason[:157] + "…" if len(reason) > 160 else reason
        return f"Blocked · {summary}"

    def unit_blocks(self) -> tuple[BlockedExecution, ...]:
        return (self,)

    def unit_block_line(self, unit: str, orders: tuple[str, ...]) -> str:
        return (f"BLOCKED {unit}: orders {', '.join(orders)}; goal {self.goal_id}; "
                f"{self.reason} -> inspect the failed work; never replay")
