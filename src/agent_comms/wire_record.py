"""Validate message records and their monotonic publication sequence."""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, BinaryIO, ClassVar

from .errors import RelationViolationError, StoredHistoryUnreadable
from .field_codec import FieldCodec, MalformedRecord
from .messages import Message

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery
    from .delivery_policy import KeyedResponseAcknowledgement
    from .wire_metadata import WireMetadata


class WireRecord(ABC):
    __slots__ = ()
    acknowledgement: KeyedResponseAcknowledgement | None = None
    max_row_bytes: ClassVar[int] = 8 * 1024 * 1024

    @classmethod
    def require_row_size(cls, row: bytes, *, message: str = "Oversized private bus row.") -> None:
        """Bound the encoded physical row, including its terminating newline."""
        if len(row) > cls.max_row_bytes:
            raise RelationViolationError(message)

    @classmethod
    def rows(cls, stream: BinaryIO, stop: int = sys.maxsize, end: int = sys.maxsize
             ) -> Iterator[tuple[int, bytes]]:
        """Complete physical rows starting before stop, read no further than end."""
        while (offset := stream.tell()) < stop:
            if not (raw := stream.readline(min(cls.max_row_bytes + 1, end - offset))):
                return
            cls.require_row_size(raw)
            if not raw.endswith(b"\n"):
                raise RelationViolationError("Incomplete bus row blocks keyed publication.")
            yield offset, raw

    @classmethod
    def from_wire(cls, value: Mapping, root_id: str) -> WireRecord:
        from .bus_publication import CommittedDelivery, has_private_wire_fields

        message = Message.from_committed_wire(value)
        if has_private_wire_fields(value):
            return CommittedDelivery.attest(message, value, root_id)
        return PublicWireRecord(message)

    @classmethod
    def public_from_wire(cls, value: object) -> WireRecord:
        """Display decoding preserves public messages and validates silent rows."""
        from .bus_publication import has_private_wire_fields
        try:
            if not isinstance(value, Mapping):
                raise MalformedRecord("Bus row must be an object")
            owner = RedactedPublicWireRecord if has_private_wire_fields(value) else PublicWireRecord
            return owner(Message.from_wire(value))
        except MalformedRecord as error:
            raise StoredHistoryUnreadable("Stored bus history cannot be decoded") from error

    def page_records(self, raw_size: int, metadata: WireMetadata) -> Iterator[tuple[Message, int]]:
        """Silent facts consume physical bytes, never message/display budgets."""
        return iter(())

    def messages(self) -> tuple[Message, ...]:
        return ()

    @abstractmethod
    def sequence_after(self, previous: int) -> int: ...

    def deliveries(self) -> tuple[CommittedDelivery, ...]:
        return ()

    def delivery_messages(self):
        from .routing import DeliveryMessage

        return tuple(DeliveryMessage(message) for message in self.messages()
                     if message.type.conversation_message)

    def record_key(self, seen: set[str]) -> None:
        if self.acknowledgement is not None:
            self.acknowledgement.add_unique(seen)

    @abstractmethod
    def require_claim(self, metadata: WireMetadata) -> None: ...

    @abstractmethod
    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int,
                        claimed_after_seq: int): ...


class MessageWireRecord(WireRecord):
    message: Message

    def messages(self) -> tuple[Message, ...]:
        return (self.message,)

    def page_records(self, raw_size: int, metadata: WireMetadata) -> Iterator[tuple[Message, int]]:
        self.require_claim(metadata)
        yield self.message, raw_size

    def sequence_after(self, previous: int) -> int:
        if self.message.seq <= previous:
            raise MalformedRecord("Bus sequence is not increasing.")
        return self.message.seq

    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int,
                        claimed_after_seq: int):
        predecessor = (self.message.seq - 1 if self.message.seq <= claimed_after_seq
                       else previous_sequence)
        return self.message.checkpoint_rows(predecessor)


@dataclass(frozen=True, slots=True)
class PublicWireRecord(MessageWireRecord):
    message: Message

    def require_claim(self, metadata: WireMetadata) -> None:
        self.message.require_retained_claim(metadata.claimed_after_seq)


class RedactedPublicWireRecord(PublicWireRecord):
    """A display projection, not attestation of the original private envelope."""

    def page_records(self, raw_size: int, metadata: WireMetadata) -> Iterator[tuple[Message, int]]:
        yield self.message, len(json.dumps(self.message.to_wire()).encode()) + 1


@dataclass
class WireScan:
    """One stream's cross-row monotonicity and publication-key checks."""

    metadata: WireMetadata
    previous_sequence: int = 0
    seen_keys: set[str] = field(default_factory=set)

    def read(self, line: bytes) -> WireRecord:
        try:
            value = FieldCodec.parse(line)
            if not isinstance(value, dict):
                raise MalformedRecord("Bus row is not an object.")
            record = WireRecord.from_wire(value, self.metadata.root_id)
            record.require_claim(self.metadata)
            record.record_key(self.seen_keys)
            self.previous_sequence = record.sequence_after(self.previous_sequence)
            return record
        except MalformedRecord as error:
            raise StoredHistoryUnreadable("Malformed public bus row blocks publication.") from error


from .delivery_policy import KeyedResponseAcknowledgement
