"""Durable pause actions without changing the rolling-compatible registry schema."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .declarations import Goal, GoalPauseSource, _atomic_write_text, _store_lock


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
class GoalPauseEvents:
    path: Path

    def snapshot(self) -> dict[str, GoalPauseEvent]:
        try:
            rows = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        return {key: GoalPauseEvent(**row) for key, row in rows.items()}

    @staticmethod
    def for_goal(goal: Goal | None, events: dict[str, GoalPauseEvent]) -> GoalPauseEvent | None:
        if goal is None or goal.status != "paused":
            return None
        return events.get(f"{goal.id}:{goal.revision}")

    def record(self, event: GoalPauseEvent) -> None:
        with _store_lock(self.path):
            events = self.snapshot()
            events[event.key] = event
            _atomic_write_text(
                self.path,
                json.dumps({key: asdict(value) for key, value in events.items()}),
                fsync_parent=True,
            )
