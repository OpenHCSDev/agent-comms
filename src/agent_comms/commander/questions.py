"""Message reads shared by unit observations and commander answer rules."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agent_comms.bus_page_index import BusPageIndex
from agent_comms.message_page import MessageReadRequest
from agent_comms.message_reference import MessageReference
from agent_comms.messages import Message, NeedsAnswerKind
from agent_comms.read_basis import RepliesDisplayScope
from agent_comms.wire_log import WireSnapshot

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.todos import Todo


@dataclass(frozen=True, slots=True)
class UnansweredUnitAsks(MessageReadRequest[tuple[Message, ...]]):
    scope: RepliesDisplayScope
    since: float
    before: float
    empty: ClassVar[tuple[Message, ...]] = ()

    @classmethod
    def for_orders(
        cls, comms: Comms, commander: str, todos: Sequence[Todo], before: float
    ) -> tuple[Message, ...]:
        if not todos:
            return ()
        snapshot = comms.registry.snapshot()
        peers = frozenset(name for todo in todos for name in snapshot.names_for(todo.held().owner))
        since = min(
            order.at.recorded_at("A held order") for todo in todos for order in todo.order_history
        )
        questions = comms.bus.log.read_page(cls(
            RepliesDisplayScope(snapshot.names_for(commander), peers), since, before,
        ))
        return tuple(message for message in questions if issubclass(message.type, NeedsAnswerKind))

    def indexed(self, index: BusPageIndex, source: WireSnapshot) -> tuple[Message, ...]:
        answered: set[MessageReference] = set()
        found: list[Message] = []
        with closing(index.recent_offsets(
            self.since, predicate=self.scope.index_predicate, before_offset=source.boundary,
        )) as offsets:
            for row in self.indexed_records(source, offsets):
                message = row.message
                if message.sender in self.scope.first_names:
                    answered.update(message.answers)
                elif message.timestamp <= self.before and message.reference not in answered:
                    found.append(message)
        return tuple(found)


@dataclass(frozen=True, slots=True)
class SenderMessages(UnansweredUnitAsks):
    """Everything the other side sent the commander in the window, answered or not."""

    def indexed(self, index: BusPageIndex, source: WireSnapshot) -> tuple[Message, ...]:
        with closing(index.recent_offsets(
            self.since, predicate=self.scope.index_predicate, before_offset=source.boundary,
        )) as offsets:
            return tuple(row.message for row in self.indexed_records(source, offsets)
                         if row.message.sender not in self.scope.first_names
                         and row.message.timestamp <= self.before)


@dataclass(frozen=True, slots=True)
class UnansweredBurst(UnansweredUnitAsks):
    """A sender whose messages arrive as one burst: an answer to any of its messages covers
    everything it sent before that answer, so the commander answers a burst once, not once per
    line. Only what it sent after the newest such answer is still open."""

    def indexed(self, index: BusPageIndex, source: WireSnapshot) -> tuple[Message, ...]:
        replied: dict[MessageReference, float] = {}
        sent: list[Message] = []
        with closing(index.recent_offsets(
            self.since, predicate=self.scope.index_predicate, before_offset=source.boundary,
        )) as offsets:
            for row in self.indexed_records(source, offsets):
                message = row.message
                if message.sender in self.scope.first_names:
                    for answered in message.answers:
                        replied[answered] = max(replied.get(answered, 0.0), message.timestamp)
                else:
                    sent.append(message)
        covered = max((replied[m.reference] for m in sent if m.reference in replied), default=0.0)
        return tuple(m for m in sent if covered < m.timestamp <= self.before)
