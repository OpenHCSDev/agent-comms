"""Transient story views rendered by the original Todo record owners."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

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


class OrderStories(ABC):
    """Find the problems an order declared, including declarations archived in its story."""

    @abstractmethod
    def _select(self, where: str, parameters: tuple) -> tuple[Todo, ...]: ...

    def for_order(self, order_id: str) -> tuple[Todo, ...]:
        from .todos import TodoError

        rows = self._for_orders((order_id,))
        if not rows:
            raise TodoError(f"Unknown saved order: {order_id!r}")
        return rows

    def related_problems(self, problem: Todo) -> tuple[Todo, ...]:
        return self._for_orders(problem.held_order_ids)

    def _for_orders(self, identities: tuple[str, ...]) -> tuple[Todo, ...]:
        from .todos import CheckedProblemOrder, StoredOrderStory, Todo

        slots = ','.join('?' for _ in identities)
        return self._select(
            f'EXISTS (SELECT 1 FROM json_tree("order") AS declaration '
            f"WHERE declaration.key='id' AND declaration.value IN ({slots})) "
            f'OR EXISTS (SELECT 1 FROM {StoredOrderStory.declared_name} AS declaration '
            f'WHERE declaration.problem={Todo.declared_name}.id '
            f'AND declaration.kind IN {CheckedProblemOrder.sql_choices()} '
            f"AND json_extract(declaration.event,'$.id') IN ({slots}))",
            (*identities, *identities))


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
    def collect(cls, order_id: str, todos: Iterable[Todo]) -> Self:
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
        lines = [f"{hold.todo.id} {hold.holder.name}: {hold.todo.order.line}" for hold in held.live]
        if held.orphaned:
            lines.append(
                f"{len(held.orphaned)} orders held by units no longer registered: "
                + ", ".join(f"{todo.id} {todo.held().owner}" for todo in held.orphaned)
            )
        return cls(tuple(lines))
