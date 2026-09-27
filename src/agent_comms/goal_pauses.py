"""Durable pause actions without changing the rolling-compatible registry schema."""

from __future__ import annotations

from dataclasses import dataclass

from .declarations import Goal, GoalPauseSource
from .locked_store import LockedStore


@dataclass(frozen=True, slots=True)
class GoalPauseEvent:
    goal_id: str
    revision: int
    source: GoalPauseSource

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", GoalPauseSource(self.source))

    @property
    def owner_instruction(self) -> str | None:
        if self.source is GoalPauseSource.OWNER:
            return (
                "This goal was paused by the owner. Do not resume or continue it; "
                "wait for the owner to explicitly resume it using the goal controls."
            )
        return None

    @property
    def key(self) -> str:
        return f"{self.goal_id}:{self.revision}"


@dataclass(frozen=True, slots=True)
class GoalPauseEvents(LockedStore[dict[str, GoalPauseEvent]]):
    @property
    def record_type(self) -> type[dict[str, GoalPauseEvent]]:
        return dict[str, GoalPauseEvent]

    def empty(self) -> dict[str, GoalPauseEvent]:
        return {}

    def snapshot(self) -> dict[str, GoalPauseEvent]:
        return self.read()

    @staticmethod
    def for_goal(goal: Goal | None, events: dict[str, GoalPauseEvent]) -> GoalPauseEvent | None:
        if goal is None or goal.status != "paused":
            return None
        return events.get(f"{goal.id}:{goal.revision}")

    def record(self, event: GoalPauseEvent) -> None:
        self.update(lambda events: {**events, event.key: event})
