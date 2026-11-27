"""Bounded sequence references to the canonical bus, never delivery authority."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agent_comms.execution.context_provenance import WireProvenance
from agent_comms.execution.turn_context import InstructionSegment
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.peer_relevance import Prioritized
from agent_comms.registry.peer_relation import PeerRelation
from agent_comms.registry.threads import Thread


@dataclass(frozen=True, kw_only=True)
class AwarenessSegment(InstructionSegment):
    """The sources a reader may consult. Each is rendered as its canonical
    `MessageReference.name`, the compact spelling a reader parses into
    `MessageReference(seq, message_id)`; `sources` keeps the references themselves."""

    bus: str
    sources: tuple[MessageReference, ...]

    @classmethod
    def capture(cls, path, records):
        sources = tuple(record.reference for record in records)
        return super().capture(tuple(WireProvenance(reference) for reference in sources),
                               bus=str(path), sources=sources)

    @classmethod
    def for_owner(
        cls, bus: Bus, owner: Thread, relations: Sequence[PeerRelation],
    ) -> tuple[AwarenessSegment, ...]:
        """Bounded frozen-recipient pointers, independent of UI read state.

        The window is the newest received rows, shown in sequence order. Peer relevance
        orders the selected rows, never changes their window.
        """
        first = {
            relation.peer.require_recorded().lookup
            for relation in relations
            if relation.relevance.grants(Prioritized)
        }
        rows = tuple(
            delivery
            for record in bus.received(
                owner.incarnation.require_recorded().lookup, limit=100,
            )
            for delivery in record.deliveries()
        )
        rows = tuple(sorted(
            rows, key=lambda row: (row.sender_lookup not in first, row.seq),
        ))
        return (cls.capture(bus.path, rows),) if rows else ()

    def values(self):
        return {"pointers": FieldCodec.compact({
            "bus": self.bus, "sources": [source.name for source in self.sources],
        }, ensure_ascii=True)}

