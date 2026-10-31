"""Detached notification evidence and the bounded window family contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from itertools import batched
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar

from pydantic import JsonValue

from .declared_family import DeclaredFamily
from .frozen_recipient import FrozenRecipient
from .field_codec import (
    FieldCodec,
    MalformedRecord,
    MessageId,
    NullMember,
    RecordMember,
    SingleFieldValue,
    SubclassUnion,
    ThreadLookup,
    boundary_decoder,
)
from .message_reference import MessageReference
from .recorded_bus import RecordedBusIdentity
from .thread_identity import ThreadIncarnation


class ConsumptionEvidence(ABC):
    """A recorded fact that may prove a delivery entered the model's context."""

    @abstractmethod
    def consumed_progress(self, progress: DeliveryProgress) -> DeliveryProgress: ...


if TYPE_CHECKING:
    from agent_comms.notification_progress import AssignmentResponse, UnansweredResponse
    from .bus_publication import Delivered
    from .comms import Comms
    from .coordination_tables.assignments import WakeAssignment
    from .notification_assignment import AssignmentActivityEvidence
    from .notification_progress import AssignmentProgress, DeliveryProgress
    from .message_notification import MessageNotification
    from .presentation import WireRevision


@dataclass(frozen=True)
class RecipientLimit(SingleFieldValue):
    """Decode one bounded recipient-window size at the request boundary."""

    minimum: ClassVar[int] = 1
    maximum: ClassVar[int] = 100
    value: int = maximum

    def __post_init__(self) -> None:
        if not self.minimum <= self.value <= self.maximum:
            raise ValueError(f"Recipient limit must be between {self.minimum} and {self.maximum}")

    @classmethod
    def schema(cls) -> dict[str, JsonValue]:
        return {"type": "integer", "minimum": cls.minimum, "maximum": cls.maximum}


class RecipientCursor(SubclassUnion, affix=""):
    """Select frozen, lookup-ordered positions before expensive evidence acquisition."""

    @abstractmethod
    def positions(self, source: Delivered) -> range: ...


@dataclass(frozen=True)
class FirstRecipient(NullMember, RecipientCursor):
    def positions(self, source: Delivered) -> range:
        return range(len(source.recipients))


@dataclass(frozen=True)
class AfterRecipient(RecordMember, RecipientCursor):
    recipient: FrozenRecipient

    def positions(self, source: Delivered) -> range:
        start = bisect_right(
            source.recipients,
            self.recipient.recipient_lookup,
            key=lambda item: item.recipient_lookup,
        )
        return range(start, len(source.recipients))


class OutcomeViewer(ABC):
    """Who reads a delivery window decides which of its outcomes are shown."""

    @abstractmethod
    def positions(self, source: Delivered) -> range: ...

    def visible_reads(
        self, displayed_to: tuple[ThreadIncarnation, ...], source: RecordedBusIdentity
    ) -> tuple[ThreadIncarnation, ...]:
        """Unscoped/operator and agent readers retain every human read mark."""
        return displayed_to

    @abstractmethod
    def sees_direct_read(self, source: Delivered) -> bool:
        pass

    @abstractmethod
    def sees(self, source: Delivered) -> bool:
        """Whether this reader may read the message at all."""


@dataclass(frozen=True, slots=True)
class AllOutcomes(OutcomeViewer):
    """An unscoped reader sees every recipient's outcome and every human read."""

    def positions(self, source: Delivered) -> range:
        return range(len(source.recipients))

    def sees_direct_read(self, source: Delivered) -> bool:
        return True

    def sees(self, source: Delivered) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class DisplayOutcomes(AllOutcomes):
    """Full operator access, minus only this display's source-matching viewer.

    Incarnation equality owns rename/recreation; the captured bus owns namespace.
    The live registry cannot establish either fact for an archived display.
    """

    viewer: ThreadIncarnation
    bus_identity: RecordedBusIdentity

    def visible_reads(
        self, displayed_to: tuple[ThreadIncarnation, ...], source: RecordedBusIdentity
    ) -> tuple[ThreadIncarnation, ...]:
        return tuple(
            reader
            for reader in displayed_to
            if reader != self.viewer or not self.bus_identity.matches(source)
        )


@dataclass(frozen=True, slots=True)
class OwnerOutcomes(OutcomeViewer):
    """A sender sees all outcomes; a recipient sees only its original outcome."""

    lookup: ThreadLookup

    @classmethod
    def of(cls, owner: ThreadIncarnation) -> OwnerOutcomes:
        return cls(owner.require_recorded().lookup)

    def positions(self, source: Delivered) -> range:
        if source.sender_lookup == self.lookup:
            return range(len(source.recipients))
        position = bisect_left(
            source.recipients, self.lookup, key=lambda item: item.recipient_lookup
        )
        if (
            position < len(source.recipients)
            and source.recipients[position].recipient_lookup == self.lookup
        ):
            return range(position, position + 1)
        return range(0)

    def sees_direct_read(self, source: Delivered) -> bool:
        return source.sender_lookup == self.lookup

    def sees(self, source: Delivered) -> bool:
        """The message's sender and its original recipients may read it."""
        return self.lookup == source.sender_lookup or bool(self.positions(source))


