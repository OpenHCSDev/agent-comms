"""Durable pause actions without changing the rolling-compatible registry schema."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .declarations import Goal
from .goal_states import PausedGoal, PauseSource
from .locked_store import LockedStore


@dataclass(frozen=True, slots=True)
class GoalPauseEvent:
    goal_id: str
    revision: int
    source: str

    @property
    def owner_instruction(self) -> str | None:
        return PauseSource.decode(self.source)().instruction()

    @property
    def key(self) -> str:
        return f"{self.goal_id}:{self.revision}"


@dataclass(frozen=True, slots=True)
class GoalPauseEvents(LockedStore[dict[str, GoalPauseEvent]]):
    filename: ClassVar[str] = "goal_pause_events.json"

    @property
    def record_type(self) -> type[dict[str, GoalPauseEvent]]:
        return dict[str, GoalPauseEvent]

    def empty(self) -> dict[str, GoalPauseEvent]:
        return {}

    def snapshot(self) -> dict[str, GoalPauseEvent]:
        return self.read()

    @staticmethod
    def for_goal(
        goal: Goal | None, events: dict[str, GoalPauseEvent] | None = None
    ) -> GoalPauseEvent | None:
        if goal is None or not isinstance(goal.state, PausedGoal):
            return None
        return GoalPauseEvent(goal.id, goal.revision, goal.state.source.declared_name)

    def record(self, event: GoalPauseEvent) -> None:
        self.update(lambda events: {**events, event.key: event})
