"""Validate wire records and track sequence numbers through complete scans.

The public message and committed delivery remain the data authorities. This
family distinguishes retained/claim envelopes from attested delivery records;
it creates no durable representation, codec or second proof store.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, BinaryIO, ClassVar

from .errors import RelationViolationError, StoredHistoryUnreadable
from .field_codec import FieldCodec, MalformedRecord
from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery
    from .delivery_policy import KeyedResponseAcknowledgement
    from .messages import Message
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
    def rows(cls, stream: BinaryIO, stop: int | None = None) -> Iterator[tuple[int, bytes]]:
        """Each complete physical row from the stream position to stop (end of file when None)."""
        while stop is None or stream.tell() < stop:
            offset = stream.tell()
            limit = cls.max_row_bytes + 1
            if not (raw := stream.readline(limit if stop is None else min(limit, stop - offset))):
                return
            cls.require_row_size(raw)
            if not raw.endswith(b"\n"):
                raise RelationViolationError("Incomplete bus row blocks keyed publication.")
            yield offset, raw

    @classmethod
    def from_wire(cls, value: Mapping, root_id: str) -> WireRecord:
        from .bus_publication import CommittedDelivery, has_private_wire_fields
        from .messages import Message

        if "observation" in value:
            return DeferredObservationWireRecord.capture(value)
        message = Message.from_committed_wire(value)
        if has_private_wire_fields(value):
            return CommittedDelivery.attest(message, value, root_id)
        return PublicWireRecord(message)

    @classmethod
    def public_from_wire(cls, value: Mapping) -> WireRecord:
        """Display decoding preserves public messages and validates silent rows."""
        from .messages import Message
        if "observation" in value:
            return DeferredObservationWireRecord.capture(value)
        return PublicWireRecord(Message.from_wire(value))

    @classmethod
    def certified_public_messages(cls, value: Mapping) -> tuple[Message, ...]:
        """Public output of a row already authorized by the complete WireScan.

        Silent observations have no public messages. Certification validates
        their envelopes and index coordinates; context readers decode payloads.
        """
        if "observation" in value:
            return ()
        return cls.public_from_wire(value).messages()

    def messages(self) -> tuple[Message, ...]:
        return ()

    def context_manifests(self):
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
    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int): ...


class MessageWireRecord(WireRecord):
    message: Message

    def messages(self) -> tuple[Message, ...]:
        return (self.message,)

    def sequence_after(self, previous: int) -> int:
        if self.message.seq <= previous:
            raise MalformedRecord("Bus sequence is not increasing.")
        return self.message.seq

    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int):
        return self.message.checkpoint_rows(previous_sequence)


class WireObservation(DeclaredFamily, affix="WireObservation"):
    """A silent fact; it has no message, audience, acknowledgement or sequence allocation."""

    @classmethod
    @abstractmethod
    def checkpoint_from_wire(cls, data: dict, offset: int, length: int, previous_sequence: int): ...

    @classmethod
    def row_label(cls, data: dict) -> str:
        return cls.declared_name

    @abstractmethod
    def context_manifests(self): ...

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

        entries = FieldCodec.decode(list[object], FieldCodec.field_data(cls, "entries", data))
        rows = []
        for entry in entries:
            manifest = FieldCodec.field_data(ContextEntryManifest, "manifest", entry)
            sources = ContextSources(FieldCodec.decode_field(SegmentManifest, "provenance", manifest))
            rows.append(ContextEntrySources.for_reference(
                ContextEntryReference.from_sources(sources), offset=offset, length=length
            ))
        return tuple(rows)

    def context_manifests(self):
        return ()

    def context_entries(self):
        return self.entries


@dataclass(frozen=True)
class RecordedContextManifestWireObservation(WireObservation):
    """One turn context referencing independently recorded transcript entries."""

    manifest: "RecordedContextManifest"

    @classmethod
    def checkpoint_from_wire(cls, data: dict, offset: int, length: int, previous_sequence: int):
        from .private_bus_checkpoint import RecordedContextManifestSources

        manifest = FieldCodec.field_data(cls, "manifest", data)
        thread = FieldCodec.decode_field(RecordedContextManifest, "thread", manifest)
        return (RecordedContextManifestSources(thread, offset=offset, length=length),)

    @classmethod
    def row_label(cls, data: dict) -> str:
        manifest = FieldCodec.field_data(cls, "manifest", data)
        sequence = FieldCodec.decode_field(RecordedContextManifest, "recording_turn", manifest)
        return f"context sequence {sequence}"

    def __post_init__(self):
        self.manifest.turn.require_recorded()

    def context_manifests(self):
        return ()

    def recorded_context_manifests(self):
        return (self.manifest,)


class ObservationWireRecord(WireRecord):
    """Silent row policy is shared by authored and deferred observation values."""

    @abstractmethod
    def read(self) -> WireObservation: ...

    def context_manifests(self):
        return self.read().context_manifests()

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

    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int):
        return DeferredObservationWireRecord.capture(self.to_wire()).checkpoint_rows(
            offset, length, previous_sequence
        )

    def to_wire(self):
        return FieldCodec.encode(self)


@dataclass(frozen=True)
class DeferredObservationWireRecord(ObservationWireRecord):
    """One owned row: index its coordinates now, decode its context only on read."""

    owner: type[WireObservation]
    data: dict

    @classmethod
    def capture(cls, value: object) -> DeferredObservationWireRecord:
        envelope = FieldCodec.record_data(AuthoredObservationWireRecord, value)
        owner, data = FieldCodec.record_member(
            WireObservation, FieldCodec.field_data(AuthoredObservationWireRecord, "observation", envelope)
        )
        return cls(owner, data)

    def read(self) -> WireObservation:
        try:
            return FieldCodec.decode_record(self.owner, self.data)
        except MalformedRecord as error:
            raise StoredHistoryUnreadable(
                f"Unreadable {self.owner.row_label(self.data)}: {error}"
            ) from error

    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int):
        return self.owner.checkpoint_from_wire(self.data, offset, length, previous_sequence)


@dataclass(frozen=True, slots=True)
class PublicWireRecord(MessageWireRecord):
    message: Message

    def require_claim(self, metadata: WireMetadata) -> None:
        self.message.require_retained_claim(metadata.claimed_after_seq)


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


from .turn_context import (
    ContextEntryManifest, ContextEntryReference, ContextSources,
    RecordedContextManifest, SegmentManifest,
)
from .delivery_policy import KeyedResponseAcknowledgement
from .messages import Message
