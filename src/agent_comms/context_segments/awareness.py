"""Include checked references to stored messages in the instruction file."""

import json
from dataclasses import dataclass

from ..private_bus_checkpoint import DeliverySources
from ..turn_context import InstructionSegment, WireProvenance


@dataclass(frozen=True, kw_only=True)
class AwarenessSegment(InstructionSegment):
    bus: str
    root_id: str
    sources: tuple[DeliverySources, ...]

    @classmethod
    def capture(cls, path, root_id, sources):
        return super().capture(tuple(WireProvenance(row.reference) for row in sources),
                               bus=str(path), root_id=root_id, sources=sources)

    def values(self):
        return {"pointers": json.dumps({
            "bus": self.bus, "root_id": self.root_id,
            "sources": [{"seq": row.seq, "message_id": row.message_id,
                         "offset": row.offset, "length": row.length} for row in self.sources],
        }, ensure_ascii=True, separators=(",", ":"))}


@dataclass(frozen=True, kw_only=True)
class UnavailableAwarenessSegment(InstructionSegment):
    @classmethod
    def instruction_name(cls):
        return "awareness-unavailable.md"

    def values(self):
        return {}
