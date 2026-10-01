"""Original certified source pointers as an instruction-file contribution."""

import json
from dataclasses import dataclass

from .private_bus_checkpoint import DeliverySources
from .turn_context import InstructionFile, InstructionSegment, WireProvenance


@dataclass(frozen=True, kw_only=True)
class AwarenessSegment(InstructionSegment):
    bus: str
    root_id: str
    sources: tuple[DeliverySources, ...]

    @classmethod
    def capture(cls, path, root_id, sources):
        instruction = InstructionFile.read("awareness.md")
        return cls(
            provenance=(instruction.source, *(WireProvenance(row.reference) for row in sources)),
            instruction=instruction, bus=str(path), root_id=root_id, sources=sources,
        )

    def values(self):
        return {"pointers": json.dumps({
            "bus": self.bus, "root_id": self.root_id,
            "sources": [{"seq": row.seq, "message_id": row.message_id,
                         "offset": row.offset, "length": row.length} for row in self.sources],
        }, ensure_ascii=True, separators=(",", ":"))}


@dataclass(frozen=True, kw_only=True)
class UnavailableAwarenessSegment(InstructionSegment):
    @classmethod
    def capture(cls):
        instruction = InstructionFile.read("awareness-unavailable.md")
        return cls(provenance=(instruction.source,), instruction=instruction)

    def values(self):
        return {}
