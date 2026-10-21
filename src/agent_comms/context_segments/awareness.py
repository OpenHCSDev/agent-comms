"""Bounded sequence references to the canonical bus, never delivery authority."""

import json
from dataclasses import dataclass

from ..field_codec import FieldCodec
from ..message_reference import MessageReference
from ..turn_context import InstructionSegment, WireProvenance


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
        return {"pointers": json.dumps({
            "bus": self.bus, "sources": [FieldCodec.encode(source) for source in self.sources],
        }, ensure_ascii=True, separators=(",", ":"))}


@dataclass(frozen=True, kw_only=True)
class UnavailableAwarenessSegment(InstructionSegment):
    @classmethod
    def instruction_name(cls):
        return "awareness-unavailable.md"

    def values(self):
        return {}
