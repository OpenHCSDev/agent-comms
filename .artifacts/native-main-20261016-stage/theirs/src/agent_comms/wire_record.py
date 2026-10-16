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
from typing import TYPE_CHECKING, ClassVar

from .errors import RelationViolationError, StoredHistoryUnreadable
from .field_codec import FieldCodec
from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery
    from .delivery_policy import KeyedResponseReceipt
    from .messages import Message
    from .wire_metadata import WireMetadata


class WireRecord(ABC):
    __slots__ = ()
    receipt: KeyedResponseReceipt | None = None
    max_row_bytes: ClassVar[int] = 8 * 1024 * 1024

    @classmethod
    def require_row_size(
        cls, row: bytes, *, error_type: type[Exception] = RelationViolationError,
        message: str = "Oversized private bus row.",
    ) -> None:
        """Bound the encoded physical row, including its terminating newline."""
        if len(row) > cls.max_row_bytes:
            raise error_type(message)

    @classmethod
    def from_wire(cls, value: Mapping, root_id: str) -> WireRecord:
        from .bus_publication import CommittedDelivery, has_private_wire_fields
        from .messages import Message

        if "observation" in value:
            return FieldCodec.decode(ObservationWireRecord, value)
        message = Message.from_committed_wire(value)
        if has_private_wire_fields(value):
            return CommittedDelivery.attest(message, value, root_id)
        return PublicWireRecord(message)

    @classmethod
    def public_from_wire(cls, value: Mapping) -> WireRecord:
        """Display decoding preserves public messages and validates silent rows."""
        from .messages import Message
        if "observation" in value:
            return FieldCodec.decode(ObservationWireRecord, value)
        return PublicWireRecord(Message.from_wire(value))

    @classmethod
    def certified_public_messages(cls, value: Mapping) -> tuple[Message, ...]:
        """Public output of a row already admitted by the complete WireScan.

        Silent observations have no public messages. Their original manifests
        were validated by certification and need no second reconstruction.
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

        return tuple(DeliveryMessage(message) for message in self.messages())

    def record_key(self, seen: set[str]) -> None:
        if self.receipt is not None:
            self.receipt.add_unique(seen)

    @abstractmethod
    def require_admission(self, metadata: WireMetadata) -> None: ...

    @abstractmethod
    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int): ...


class MessageWireRecord(WireRecord):
    message: Message

    def messages(self) -> tuple[Message, ...]:
        return (self.message,)

    def sequence_after(self, previous: int) -> int:
        if self.message.seq <= previous:
            raise ValueError("Bus sequence is not increasing.")
        return self.message.seq

    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int):
        return self.message.checkpoint_rows(previous_sequence)


class WireObservation(DeclaredFamily, affix="WireObservation"):
    """A silent fact; it has no message, audience, receipt or sequence allocation."""

    @abstractmethod
    def context_manifests(self): ...

    def context_entries(self):
        return ()

    def recorded_context_manifests(self):
        return ()

    @abstractmethod
    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int): ...


@dataclass(frozen=True)
class ContextEntryManifestWireObservation(WireObservation):
    """Transcript entry manifests recorded independently of turn membership."""

    entries: tuple["ContextEntryManifest", ...]

    def context_manifests(self):
        return ()

    def context_entries(self):
        return self.entries

    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int):
        from .private_bus_checkpoint import ContextEntrySources

        return tuple(
            ContextEntrySources.for_entry(entry, offset=offset, length=length)
            for entry in self.entries
        )


@dataclass(frozen=True)
class RecordedContextManifestWireObservation(WireObservation):
    """One turn context referencing independently recorded transcript entries."""

    manifest: "RecordedContextManifest"

    def __post_init__(self):
        self.manifest.turn.require_recorded()

    def context_manifests(self):
        return ()

    def recorded_context_manifests(self):
        return (self.manifest,)

    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int):
        from .private_bus_checkpoint import RecordedContextManifestSources

        return (
            RecordedContextManifestSources(
                self.manifest.thread, offset=offset, length=length
            ),
        )


@dataclass(frozen=True)
class ObservationWireRecord(WireRecord):
    observation: WireObservation

    def context_manifests(self):
        return self.observation.context_manifests()

    def context_entries(self):
        return self.observation.context_entries()

    def recorded_context_manifests(self):
        return self.observation.recorded_context_manifests()

    def sequence_after(self, previous: int) -> int:
        return previous

    def require_admission(self, metadata: WireMetadata) -> None:
        # Decoding validates the observation; it grants no delivery admission.
        return None

    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int):
        return self.observation.checkpoint_rows(offset, length, previous_sequence)

    def to_wire(self):
        return FieldCodec.encode(self)


@dataclass(frozen=True, slots=True)
class PublicWireRecord(MessageWireRecord):
    message: Message

    def require_admission(self, metadata: WireMetadata) -> None:
        self.message.require_retained_admission(metadata.admission_after_seq)


@dataclass
class WireScan:
    """One stream's cross-row monotonicity and publication-key authority."""

    metadata: WireMetadata
    previous_sequence: int = 0
    seen_keys: set[str] = field(default_factory=set)

    def read(self, line: bytes) -> WireRecord:
        from .bus_publication import unique_wire_object

        WireRecord.require_row_size(line)
        if not line.endswith(b"\n"):
            raise RelationViolationError("Incomplete bus row blocks keyed publication.")
        try:
            value = json.loads(line, object_pairs_hook=unique_wire_object)
            if not isinstance(value, dict):
                raise ValueError("Bus row is not an object.")
            record = WireRecord.from_wire(value, self.metadata.root_id)
            record.require_admission(self.metadata)
            record.record_key(self.seen_keys)
            self.previous_sequence = record.sequence_after(self.previous_sequence)
            return record
        except RelationViolationError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
            raise StoredHistoryUnreadable("Malformed public bus row blocks publication.") from error


from .turn_context import ContextEntryManifest, RecordedContextManifest
from .delivery_policy import KeyedResponseReceipt
from .messages import Message
