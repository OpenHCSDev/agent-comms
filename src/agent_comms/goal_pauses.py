"""Typed durable pause events with one saved JSON boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Mapping

from .field_codec import FieldCodec, projected
from .goal_states import PausedGoal, PauseSource
from .goals import Goal
from .locked_store import LockedStore


@dataclass(frozen=True, slots=True)
class GoalPauseEvent:
    goal_id: str
    revision: int
    source: PauseSource = field(metadata={"wire_exclude": True})

    @projected(view="wire", name="source")
    def wire_source(self) -> str:
        return self.source.declared_name

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.project(self, "wire")

    @classmethod
    def from_wire(cls, data: Mapping) -> GoalPauseEvent:
        values = dict(data)
        values["source"] = {"kind": values["source"]}
        return FieldCodec.decode(cls, values)

    @property
    def owner_instruction(self) -> str | None:
        return self.source.instruction()

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

    def _decode(self, data: dict) -> dict[str, GoalPauseEvent]:
        return {key: GoalPauseEvent.from_wire(row) for key, row in data.items()}

    def _encode(self, value: dict[str, GoalPauseEvent]) -> dict:
        return {key: event.to_wire() for key, event in value.items()}

    @staticmethod
    def for_goal(goal: Goal | None) -> GoalPauseEvent | None:
        if goal is None or not isinstance(goal.state, PausedGoal):
            return None
        return GoalPauseEvent(goal.id, goal.revision, goal.state.source)

    def record(self, event: GoalPauseEvent) -> None:
        self.update(lambda events: {**events, event.key: event})
