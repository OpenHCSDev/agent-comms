"""Canonical wire-record admission and complete-scan sequence custody.

The public message and committed delivery remain the data authorities. This
family distinguishes retained/claim envelopes from attested delivery records;
it creates no durable representation, codec or second proof store.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .errors import RelationViolationError

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery
    from .delivery_policy import KeyedResponseReceipt
    from .messages import Message
    from .wire_metadata import WireMetadata


class WireRecord(ABC):
    __slots__ = ()
    message: Message
    receipt: KeyedResponseReceipt | None = None

    @classmethod
    def from_wire(cls, value: Mapping, root_id: str) -> WireRecord:
        from .bus_publication import CommittedDelivery, has_private_wire_fields
        from .messages import Message

        message = Message.from_committed_wire(value)
        if has_private_wire_fields(value):
            return CommittedDelivery.attest(message, value, root_id)
        return PublicWireRecord(message)

    def deliveries(self) -> tuple[CommittedDelivery, ...]:
        return ()

    def record_key(self, seen: set[str]) -> None:
        if self.receipt is not None:
            self.receipt.add_unique(seen)

    @abstractmethod
    def require_admission(self, metadata: WireMetadata) -> None: ...

    @abstractmethod
    def checkpoint_rows(self, offset: int, length: int): ...


@dataclass(frozen=True, slots=True)
class PublicWireRecord(WireRecord):
    message: Message

    def require_admission(self, metadata: WireMetadata) -> None:
        self.message.require_retained_admission(metadata.admission_after_seq)

    def checkpoint_rows(self, offset: int, length: int):
        return ()


@dataclass
class WireScan:
    """One stream's cross-row monotonicity and publication-key authority."""

    metadata: WireMetadata
    previous_sequence: int = 0
    seen_keys: set[str] = field(default_factory=set)
    max_row_bytes = 8 * 1024 * 1024

    def read(self, line: bytes) -> WireRecord:
        from .bus_publication import DuplicateWireKeyError, unique_wire_object

        if len(line) > self.max_row_bytes:
            raise RelationViolationError("Oversized private bus row.")
        if not line.endswith(b"\n"):
            raise RelationViolationError("Incomplete bus row blocks keyed publication.")
        try:
            value = json.loads(line, object_pairs_hook=unique_wire_object)
            if not isinstance(value, dict):
                raise ValueError("Bus row is not an object.")
            record = WireRecord.from_wire(value, self.metadata.root_id)
            if record.message.seq <= self.previous_sequence:
                raise ValueError("Bus sequence is not increasing.")
            record.require_admission(self.metadata)
            record.record_key(self.seen_keys)
            self.previous_sequence = record.message.seq
            return record
        except (RelationViolationError, DuplicateWireKeyError):
            raise
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
            raise RelationViolationError("Malformed public bus row blocks publication.") from error
