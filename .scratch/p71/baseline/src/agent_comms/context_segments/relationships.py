"""A thread's relations to its peers, shown in its context beside its instructions."""

import hashlib
import json
from dataclasses import dataclass

from ..field_codec import FieldCodec
from ..relationships import RelationRecord
from ..turn_context import InstructionFile, InstructionSegment, OwnerProvenance


@dataclass(frozen=True, kw_only=True)
class RelationshipsSegment(InstructionSegment):
    records: tuple[RelationRecord, ...]

    @classmethod
    def capture(cls, owner, relations):
        """Every relation ``owner`` has, in the order its peers are listed."""
        records = tuple(record for relation in relations for record in relation.records)
        if not records:
            return ()
        instruction = InstructionFile.read("relationships.md")
        revision = hashlib.sha256(json.dumps(FieldCodec.encode(records)).encode()).hexdigest()
        return (cls(
            provenance=(instruction.source, OwnerProvenance(owner.incarnation, revision)),
            instruction=instruction, records=records,
        ),)

    def values(self):
        return {"records": json.dumps(FieldCodec.encode(self.records), separators=(",", ":"))}
