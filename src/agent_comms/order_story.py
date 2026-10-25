"""Transient story views rendered by the original Todo record owners."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from .build_identity import InstalledBuild
from .field_codec import OrderId
from .recorded_time import EventTime
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .held_orders import HeldOrders
    from .todos import StrikeTodo, Todo


@dataclass(frozen=True)
class StoryLine:
    at: EventTime
    text: str


class StorySource:
    def implied_strikes(self) -> tuple[StrikeTodo, ...]:
        return ()

    def strikes_voided(self) -> tuple[StrikeTodo, ...]:
        return ()

    def history_events(self, current: Todo) -> tuple[Self | StrikeTodo, ...]:
        return tuple(event for strike in self.implied_strikes()
                     for event in strike.history_events(current))

    def story(self) -> tuple[StoryLine, ...]:
        return ()


class StoryEvent(StorySource):
    at: EventTime

    def history_events(self, current: Todo) -> tuple[Self | StrikeTodo, ...]:
        return (*super().history_events(current), self)

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
    order_id: OrderId
    problems: tuple[ProblemStory, ...]

    @classmethod
    def collect(cls, order_id: OrderId, todos: Iterable[Todo]) -> Self:
        """The requested declaration keeps its identity after its problems are reissued."""
        return cls(order_id, tuple(todo.story() for todo in todos))

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
        installed = InstalledBuild.read()
        lines = [f"{hold.todo.id} {hold.holder.name}: {hold.todo.order.installed_line(installed)}"
                 for hold in held.live]
        if held.orphaned:
            lines.append(
                f"{len(held.orphaned)} orders held by units no longer registered: "
                + ", ".join(f"{todo.id} {todo.held().owner}" for todo in held.orphaned)
            )
        return cls(tuple(lines))
