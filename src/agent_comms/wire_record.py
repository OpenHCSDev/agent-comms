"""Validate wire records and track sequence numbers through complete scans.

The public message and committed delivery remain the data authorities. This
family distinguishes retained/claim envelopes from attested delivery records;
it creates no durable representation, codec or second proof store.
"""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, BinaryIO, ClassVar, cast

from .declared_family import DeclaredFamily
from .errors import RelationViolationError, StoredHistoryUnreadable
from .field_codec import FieldCodec, MalformedRecord, WireValue
from .messages import Message
from .thread_identity import ThreadIncarnation

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
        """Each complete physical row starting before stop, read no further than end.

        Only yielded rows are read, so the stream rests at the end of the last one.
        """
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

        if "observation" in value:
            return DeferredObservationWireRecord.decode(value)
        message = Message.from_committed_wire(value)
        if has_private_wire_fields(value):
            return CommittedDelivery.attest(message, value, root_id)
        return PublicWireRecord(message)

    @classmethod
    def public_from_wire(cls, value: object) -> WireRecord:
        """Display decoding preserves public messages and validates silent rows."""
        from .bus_publication import has_private_wire_fields
        if not isinstance(value, Mapping):
            raise MalformedRecord("Bus row must be an object")
        if "observation" in value:
            return DeferredObservationWireRecord.decode(value)
        owner = RedactedPublicWireRecord if has_private_wire_fields(value) else PublicWireRecord
        return owner(Message.from_wire(value))

    @classmethod
    def public_page_records(cls, value: object, raw_size: int,
                            metadata: WireMetadata) -> Iterator[tuple[Message, int]]:
        try:
            record = cls.public_from_wire(value)
        except MalformedRecord as error:
            raise StoredHistoryUnreadable("Stored bus history cannot be decoded") from error
        yield from record.page_records(raw_size, metadata)

    def page_records(self, raw_size: int, metadata: WireMetadata) -> Iterator[tuple[Message, int]]:
        """Silent facts consume physical bytes, never message/display budgets."""
        return iter(())

    def messages(self) -> tuple[Message, ...]:
        return ()

    def context_entries(self):
        return ()

    def recorded_context_manifests(self):
        return ()

    @abstractmethod
    def sequence_after(self, previous: int) -> int: ...

    def deliveries(self) -> tuple[CommittedDelivery, ...]:
        return ()

    def delivery_messages(self):
        from .routing import DeliveryMessage

        return tuple(
            DeliveryMessage(message) for message in self.messages()
            if message.type.conversation_message
        )

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
        # Retained messages precede this root's own allocations. Their source
        # positions stay intact; only this root's suffix can contain an UNKNOWN gap.
        predecessor = (self.message.seq - 1 if self.message.seq <= claimed_after_seq
                       else previous_sequence)
        return self.message.checkpoint_rows(predecessor)


class WireObservation(DeclaredFamily, affix="WireObservation"):
    """A silent fact; it has no message, audience, acknowledgement or sequence allocation."""

    @classmethod
    @abstractmethod
    def checkpoint_from_wire(
        cls, data: dict, offset: int, length: int, previous_sequence: int
    ) -> tuple: ...

    @classmethod
    def row_label(cls, data: dict) -> str:
        return cls.declared_name

    def context_entries(self):
        return ()

    def recorded_context_manifests(self):
        return ()



@dataclass(frozen=True)
class ContextEntryManifestWireObservation(WireObservation):
    """Transcript entry manifests recorded independently of turn membership."""

    entries: tuple["ContextEntryManifest", ...]

    @classmethod
    def checkpoint_from_wire(cls, data: dict, offset: int, length: int, previous_sequence: int):
        from .private_bus_checkpoint import ContextEntrySources

        entries = FieldCodec.decode(list[object], FieldCodec.field_plan(cls, "entries").raw(data))
        rows = []
        for entry in entries:
            manifest = FieldCodec.field_plan(ContextEntryManifest, "manifest").raw(entry)
            provenance = FieldCodec.field_plan(SegmentManifest, "provenance").read(manifest)
            sources = ContextSources(cast(tuple, provenance))
            rows.append(ContextEntrySources.for_reference(
                ContextEntryReference.from_sources(sources), offset=offset, length=length
            ))
        return tuple(rows)

    def context_entries(self):
        return self.entries


@dataclass(frozen=True)
class RecordedContextManifestWireObservation(WireObservation):
    """One turn context referencing independently recorded transcript entries."""

    manifest: "RecordedContextManifest"

    @classmethod
    def checkpoint_from_wire(cls, data: dict, offset: int, length: int, previous_sequence: int):
        from .private_bus_checkpoint import RecordedContextManifestSources

        manifest = FieldCodec.field_plan(cls, "manifest").raw(data)
        thread = FieldCodec.field_plan(RecordedContextManifest, "thread").read(manifest)
        return (RecordedContextManifestSources(
            cast(ThreadIncarnation, thread), offset=offset, length=length),)

    @classmethod
    def row_label(cls, data: dict) -> str:
        manifest = FieldCodec.field_plan(cls, "manifest").raw(data)
        sequence = FieldCodec.field_plan(RecordedContextManifest, "recording_turn").read(manifest)
        return f"context sequence {sequence}"

    def __post_init__(self):
        self.manifest.turn.require_recorded()

    def recorded_context_manifests(self):
        return (self.manifest,)


class ObservationWireRecord(WireRecord):
    """Silent row policy is shared by authored and deferred observation values."""

    @abstractmethod
    def read(self) -> WireObservation: ...

    def context_entries(self):
        return self.read().context_entries()

    def recorded_context_manifests(self):
        return self.read().recorded_context_manifests()

    def sequence_after(self, previous: int) -> int:
        return previous

    def require_claim(self, metadata: WireMetadata) -> None:
        # An observation never authorizes delivery or allocates a message sequence.
        return None


@dataclass(frozen=True)
class AuthoredObservationWireRecord(ObservationWireRecord):
    observation: WireObservation

    def read(self) -> WireObservation:
        return self.observation

    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int,
                        claimed_after_seq: int) -> tuple:
        return DeferredObservationWireRecord.decode(self.to_wire()).checkpoint_rows(
            offset, length, previous_sequence, claimed_after_seq
        )

    def to_wire(self) -> dict:
        return cast(dict, FieldCodec.encode(self))


@dataclass(frozen=True)
class DeferredObservationWireRecord(ObservationWireRecord, WireValue):
    """The original row is decoded only for the selected context read."""

    data: dict

    @classmethod
    def decode(cls, value: object) -> DeferredObservationWireRecord:
        record = cls(FieldCodec.record_data(AuthoredObservationWireRecord, value))
        _owner, _data = record.member
        return record

    @property
    def member(self) -> tuple[type[WireObservation], dict]:
        plan = FieldCodec.field_plan(AuthoredObservationWireRecord, "observation")
        return FieldCodec.record_member(plan.annotation, plan.raw(self.data))

    def to_wire(self) -> dict:
        return self.data

    def read(self) -> WireObservation:
        owner, data = self.member
        try:
            return FieldCodec.decode_record(owner, data)
        except MalformedRecord as error:
            raise StoredHistoryUnreadable(f"Unreadable {owner.row_label(data)}: {error}") from error

    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int,
                        claimed_after_seq: int) -> tuple:
        owner, data = self.member
        return owner.checkpoint_from_wire(data, offset, length, previous_sequence)


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
    """One stream's cross-row monotonicity and publication-key authority."""

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
from .turn_context import (
    ContextEntryManifest,
    ContextEntryReference,
    ContextSources,
    RecordedContextManifest,
    SegmentManifest,
)
