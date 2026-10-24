"""Message reads shared by unit observations and commander answer rules."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agent_comms.bus import BusReader
from agent_comms.message_page import MessageReadRequest
from agent_comms.messages import Message, MessageKind, NeedsAnswerKind
from agent_comms.read_basis import RepliesDisplayScope

if TYPE_CHECKING:
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.thread_identity import ThreadIncarnation
    from agent_comms.todos import Todo


@dataclass(frozen=True, slots=True)
class SenderMessages(MessageReadRequest[tuple[Message, ...]]):
    """Messages from the other side, with one captured SQL paging lifetime."""

    scope: RepliesDisplayScope
    since: float
    before: float
    empty: ClassVar[tuple[Message, ...]] = ()

    def lower_bound(self, source: BusReader) -> float:
        return self.since

    def page(self, source: BusReader, lower: float, upper: int) -> tuple[Message, ...]:
        requesters = tuple(self.scope.second_names - self.scope.first_names)
        marks = ",".join("?" for _ in requesters)
        predicate = f"sender IN ({marks}) AND " + Message.table._sql(
            "{timestamp} >= ? AND {timestamp} <= ?")
        return source.scoped_page(
            predicate, (*requesters, lower, self.before), upper=upper, descending=True,
        )

    def read_opened(self, source: BusReader) -> tuple[Message, ...]:
        found: list[Message] = []
        with source.reading() as reader:
            lower = self.lower_bound(reader)
            upper = reader.revision() + 1
            while page := self.page(reader, lower, upper):
                found.extend(message for message in page if self.scope.includes(message))
                upper = page[-1].seq
        return tuple(found)


@dataclass(frozen=True, slots=True)
class UnansweredUnitAsks(SenderMessages):
    """Individual requests without an answer to their exact original reference."""

    @classmethod
    def for_orders(
        cls, source: BusReader, snapshot: RegistrySnapshot, commander: str,
        held: Mapping[ThreadIncarnation, Sequence[Todo]], before: float,
    ) -> dict[ThreadIncarnation, tuple[Message, ...]]:
        from agent_comms.todos import OrderClaim

        if not held:
            return {}
        peers = frozenset(name for owner in held for name in snapshot.names_for(owner.name))
        since = min(
            order.at.recorded_at("A held order")
            for todos in held.values() for todo in todos for order in todo.order_history
        )
        questions = cls(
            RepliesDisplayScope(snapshot.names_for(commander), peers), since, before,
        ).read_opened(source)
        claims = OrderClaim.originals(
            source,
            {owner: tuple(claim for todo in todos for claim in todo.order.commander_claims())
             for owner, todos in held.items()},
        )
        return {
            owner: (
                *(message for message in questions
                  if snapshot.canonical_name(message.sender) == owner.name),
                *claims.get(owner, ()),
            )
            for owner in held
        }

    def page(self, source: BusReader, lower: float, upper: int) -> tuple[Message, ...]:
        return source.unanswered_page(
            self.scope,
            since=lower, before=self.before, upper=upper,
            kinds=MessageKind.members_with(NeedsAnswerKind),
        )


@dataclass(frozen=True, slots=True)
class UnansweredBurst(SenderMessages):
    """An answer to one burst line covers everything sent before that answer."""

    def lower_bound(self, source: BusReader) -> float:
        covered = source.answer_timestamp(
            self.scope,
            since=self.since,
        )
        return max(self.since, math.nextafter(covered, math.inf))
