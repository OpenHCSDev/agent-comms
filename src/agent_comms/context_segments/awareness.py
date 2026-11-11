"""Bounded sequence references to the canonical bus, never delivery authority."""

from dataclasses import dataclass

from agent_comms.execution.turn_context import InstructionSegment, WireProvenance
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import FieldCodec


@dataclass(frozen=True, kw_only=True)
class AwarenessSegment(InstructionSegment):
    bus: str
    sources: tuple[MessageReference, ...]

    @classmethod
    def capture(cls, path, records):
        sources = tuple(record.reference for record in records)
        return super().capture(tuple(WireProvenance(reference) for reference in sources),
                               bus=str(path), sources=sources)

    def values(self):
        return {"pointers": FieldCodec.compact({
            "bus": self.bus, "sources": [FieldCodec.encode(source) for source in self.sources],
        }, ensure_ascii=True)}

