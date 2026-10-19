"""What a unit did during its order, read from its own turn records."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .order_story import StoryLine
from .thread_identity import ThreadIncarnation
from .tool_result import ToolResult
from .transcript_events import OrderCall, TurnDigest
from .turn_digests import TurnsSince

if TYPE_CHECKING:
    from .comms import Comms


class OrderWorkResult(ToolResult, ABC):
    """A problem's work read includes an explicit unassigned result."""

    @classmethod
    def for_problem(cls, comms: Comms, problem: str) -> OrderWorkResult:
        comms.goals.todos.get(problem)  # Unknown problems are not unassigned orders.
        return next((OrderWork.for_assigned(comms, todo.id, todo.held().owner,
                                           todo.order.issued().at.recorded_at("An order"))
                     for todo in comms.goals.todos.assigned_problem(problem)),
                    UnassignedOrderWork(problem))

    @abstractmethod
    def story(self) -> tuple[StoryLine, ...]: ...


@dataclass(frozen=True)
class UnassignedOrderWork(OrderWorkResult):
    problem: str

    def story(self) -> tuple[StoryLine, ...]:
        return ()


@dataclass(frozen=True)
class OrderWork(OrderWorkResult):
    problem: str
    thread: ThreadIncarnation
    since: float
    calls: tuple[OrderCall, ...]

    def story(self) -> tuple[StoryLine, ...]:
        return tuple(event for call in self.calls for event in call.story(self.problem))

    @classmethod
    def for_assigned(cls, comms: Comms, problem: str, owner: str, since: float) -> OrderWork:
        turns = comms.transcripts.turns(owner, TurnsSince(since))
        return cls.read(problem, turns.thread, since, turns.turns)

    @classmethod
    def read(
        cls, problem: str, thread: ThreadIncarnation, since: float, turns: tuple[TurnDigest, ...]
    ) -> OrderWork:
        """Each call once by tool_call_id, newest first."""
        calls: dict[str, OrderCall] = {}
        for turn in reversed(turns):
            for event in turn.events:
                event.record_call(calls, since)
        return cls(problem, thread, since, tuple(reversed(calls.values())))
