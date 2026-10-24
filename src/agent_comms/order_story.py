"""Transient story views rendered by the original Todo record owners."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from .recorded_time import EventTime
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .held_orders import HeldOrders
    from .todos import Todo


@dataclass(frozen=True)
class StoryLine:
    at: EventTime
    text: str


class StorySource:
    def story(self) -> tuple[StoryLine, ...]:
        return ()


class StoryEvent(StorySource):
    at: EventTime

    @abstractmethod
    def story_text(self) -> str: ...

    def story(self) -> tuple[StoryLine, ...]:
        return (StoryLine(self.at, self.story_text()),)


@dataclass(frozen=True)
class ProblemStory:
    problem: str
    state: str
    events: tuple[StoryLine, ...]

    @property
    def line(self) -> str:
        return f"{self.problem}: {self.state}; " + "; ".join(
            " ".join(event.text.split()) for event in self.events
        )


@dataclass(frozen=True)
class OrderStory(ToolResult):
    order_id: str
    problems: tuple[ProblemStory, ...]

    @classmethod
    def collect(cls, todos: Iterable[Todo]) -> tuple[Self, ...]:
        grouped: dict[str, list[ProblemStory]] = {}
        for todo in todos:
            grouped.setdefault(todo.order.checked().id, []).append(todo.story())
        return tuple(cls(order_id, tuple(problems)) for order_id, problems in grouped.items())

    @property
    def line(self) -> str:
        return " | ".join(problem.line for problem in self.problems)


@dataclass(frozen=True)
class FleetStory(ToolResult):
    """What each held order is doing now: one line per order a registered unit holds, then one
    line naming the orders whose holder is no longer registered. A held order's past verdicts
    are its order story."""

    lines: tuple[str, ...]

    @classmethod
    def collect(cls, held: HeldOrders) -> Self:
        lines = [f"{hold.todo.id} {hold.holder.name}: {hold.todo.order.line}" for hold in held.live]
        if held.orphaned:
            lines.append(
                f"{len(held.orphaned)} orders held by units no longer registered: "
                + ", ".join(f"{todo.id} {todo.held().owner}" for todo in held.orphaned)
            )
        return cls(tuple(lines))
