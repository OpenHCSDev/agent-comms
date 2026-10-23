"""A thread's relations to its peers, shown in its context beside its instructions."""

import hashlib
import json
from dataclasses import dataclass

from ..field_codec import FieldCodec
from ..relationships import RelationRecord
from ..turn_context import InstructionSegment, OwnerProvenance


@dataclass(frozen=True, kw_only=True)
class RelationshipsSegment(InstructionSegment):
    records: tuple[RelationRecord, ...]

    @classmethod
    def capture(cls, owner, relations):
        """Every relation ``owner`` has, in the order its peers are listed."""
        records = tuple(record for relation in relations for record in relation.records)
        if not records:
            return ()
        revision = hashlib.sha256(json.dumps(FieldCodec.encode(records)).encode()).hexdigest()
        return (super().capture((OwnerProvenance(owner.incarnation, revision),), records=records),)

    def values(self):
        return {"records": FieldCodec.compact(FieldCodec.encode(self.records))}
