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
    from .bus_publication import Delivered
    from .comms import Comms
    from .coordination_tables.assignments import WakeAssignment
    from .notification_assignment import AssignmentActivityEvidence
    from .notification_progress import AssignmentProgress, DeliveryProgress
    from .presentation import MessageNotification, WireRevision


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


@dataclass(frozen=True)
class RecipientSelection:
    """A bounded slice of original frozen positions, never a replacement audience."""

    source: Delivered
    eligible: range
    limit: RecipientLimit

    @property
    def positions(self) -> range:
        return self.eligible[: self.limit.value]

    @property
    def remaining(self) -> int:
        return len(self.eligible) - len(self.positions)

    @property
    def recipients(self) -> tuple[FrozenRecipient, ...]:
        return tuple(self.source.recipients[position] for position in self.positions)

    @property
    def entries(
        self,
    ) -> tuple[tuple[tuple[ThreadLookup, MessageReference], FrozenRecipient], ...]:
        return tuple(
            ((recipient.recipient_lookup, self.source.message.reference), recipient)
            for recipient in self.recipients
        )


@dataclass(frozen=True)
class NotificationSelection:
    """The exact per-original recipient cuts used by every expensive evidence reader."""

    viewer: OutcomeViewer
    pages: tuple[RecipientSelection, ...]

    @property
    def sources(self) -> tuple[Delivered, ...]:
        return tuple(page.source for page in self.pages)

    @property
    def keys(self) -> tuple[tuple[ThreadLookup, MessageReference], ...]:
        return tuple(key for page in self.pages for key, _recipient in page.entries)


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

    def selection(
        self,
        sources: Sequence[Delivered],
        *,
        after: RecipientCursor = FirstRecipient(),
        limit: RecipientLimit = RecipientLimit(),
    ) -> NotificationSelection:
        pages = []
        for source in sources:
            eligible, following = self.positions(source), after.positions(source)
            pages.append(
                RecipientSelection(
                    source,
                    range(max(eligible.start, following.start), min(eligible.stop, following.stop)),
                    limit,
                )
            )
        return NotificationSelection(self, tuple(pages))

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


@dataclass(frozen=True)
class AssignmentResponse(DeclaredFamily, affix="Response"):
    def recorded(self, message: Delivered) -> AssignmentResponse:
        return self

    @classmethod
    @boundary_decoder
    def from_columns(
        cls, seq: object, message_id: object, received_at_ms: object
    ) -> AssignmentResponse:
        """SQL NULL is the one foreign fact tested; present values decode as their own types."""
        columns = (seq, message_id, received_at_ms)
        if all(column is None for column in columns):
            return UnansweredResponse()
        if any(column is None for column in columns):
            raise MalformedRecord("Published response evidence is incomplete")
        return AnsweredResponse(
            MessageReference(FieldCodec.decode(int, seq), FieldCodec.decode(MessageId, message_id)),
            FieldCodec.decode(int, received_at_ms),
        )

    def detail_texts(self) -> tuple[str, ...]:
        return ()

    @abstractmethod
    def completed_notification(
        self, progress: AssignmentProgress, notification: MessageNotification
    ) -> MessageNotification: ...

    @abstractmethod
    def answered(self, notification: MessageNotification) -> MessageNotification:
        """The notification with this recorded reply attached; consumption stays its proof's."""


class UnansweredResponse(AssignmentResponse):
    def recorded(self, message: Delivered) -> AssignmentResponse:
        return AnsweredResponse(message.reference, message.at_ms)

    def completed_notification(
        self, progress: AssignmentProgress, notification: MessageNotification
    ) -> MessageNotification:
        return progress.consumed_notification(notification)

    def answered(self, notification: MessageNotification) -> MessageNotification:
        return notification


@dataclass(frozen=True)
class AnsweredResponse(AssignmentResponse):
    reference: MessageReference
    received_at_ms: int

    def completed_notification(
        self, progress: AssignmentProgress, notification: MessageNotification
    ) -> MessageNotification:
        return progress.answered_notification(notification, self)

    def answered(self, notification: MessageNotification) -> MessageNotification:
        return notification.state.answered_notification(notification, self)

    def detail_texts(self) -> tuple[str, ...]:
        return (f"Answer #{self.reference.seq} · {self.reference.message_id}",)


@dataclass(frozen=True)
class SourceEvidence:
    """One original's claims, harness attempts, stored inputs and independent responses,
    grouped by frozen recipient. Equality over all of them is the window's change test."""

    claims: tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...] = ()
    consumption: Mapping[ThreadLookup, tuple[ConsumptionEvidence, ...]] = field(
        default_factory=dict
    )
    responses: Mapping[ThreadLookup, AssignmentResponse] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def answered(
        self, recipient: ThreadLookup, notification: MessageNotification
    ) -> MessageNotification:
        return self.responses.get(recipient, UnansweredResponse()).answered(notification)

    def consumed_progress(self, recipient: ThreadLookup,
                          progress: DeliveryProgress) -> DeliveryProgress:
        """The recipient's progress after each of its own harness attempts and stored inputs
        of this original."""
        for attempt in self.consumption.get(recipient, ()):
            progress = attempt.consumed_progress(progress)
        return progress


@dataclass(frozen=True)
class NotificationAssignmentRead:
    """A window's claims and its harness attempts, both keyed by the exact original, as the
    viewer captured with them may see them: every later projection and change check of this
    read answers for that same viewer.

    A harness attempt is its own consumption evidence: a no-wake or passive recipient has no
    claim for the attempt to move, and its message still entered the session."""

    selection: NotificationSelection
    rows: tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...]
    consumption: Mapping[tuple[ThreadLookup, MessageReference], tuple[ConsumptionEvidence, ...]] = (
        field(default_factory=lambda: MappingProxyType({}))
    )
    responses: Mapping[tuple[ThreadLookup, MessageReference], AssignmentResponse] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def sources(self) -> tuple[Delivered, ...]:
        return self.selection.sources

    @cached_property
    def evidence(self) -> Mapping[MessageReference, SourceEvidence]:
        """Each original's evidence, grouped once per captured window."""
        claims: dict[MessageReference, list[tuple[WakeAssignment, AssignmentActivityEvidence]]] = {
            source.message.reference: [] for source in self.sources
        }
        consumption: dict[MessageReference, dict[ThreadLookup, tuple[ConsumptionEvidence, ...]]] = {
            source.message.reference: {} for source in self.sources
        }
        responses: dict[MessageReference, dict[ThreadLookup, AssignmentResponse]] = {
            source.message.reference: {} for source in self.sources
        }
        for (lookup, source), response in self.responses.items():
            responses[source][lookup] = response
        for (lookup, source), facts in self.consumption.items():
            consumption[source][lookup] = facts
        for row in self.rows:
            claims.setdefault(row[0].source, []).append(row)
        return {
            source: SourceEvidence(
                tuple(claims[source]),
                MappingProxyType(consumption[source]),
                MappingProxyType(responses[source]),
            )
            for source in claims
        }

    def differs_from(self, fresh: Mapping[MessageReference, SourceEvidence]) -> bool:
        return any(previous != fresh.get(source, SourceEvidence())
                   for source, previous in self.evidence.items())


@dataclass(frozen=True)
class NotificationReadMark:
    """The exact display evidence used for a source/recipient, including no display.

    A mark is identified by its source's message reference, not the source's value:
    bus rows are append-only (typed_table's abort-on-UPDATE/DELETE triggers), so a
    reference names one frozen delivery and its audience. Comparing the delivery
    itself walked its whole audience once per mark, so a window of N marks on an
    N-recipient channel message compared N squared recipients on every revision.
    """

    source: Delivered = field(compare=False)
    recipient: FrozenRecipient
    displayed_to: tuple[ThreadIncarnation, ...]
    reference: MessageReference = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference", self.source.message.reference)


@dataclass(frozen=True)
class NotificationWindow(ABC):
    """One window owns its evidence, batching and change contract."""

    window_limit: ClassVar[int] = 120
    outcomes: Mapping[tuple[int, str], tuple[MessageNotification, ...]]
    reads: tuple[NotificationAssignmentRead, ...]
    read_marks: tuple[NotificationReadMark, ...] = ()

    def remaining(self, reference: MessageReference) -> int:
        return next(
            (
                page.remaining
                for read in self.reads
                for page in read.selection.pages
                if page.source.message.reference == reference
            ),
            0,
        )

    @classmethod
    def chunks[T](cls, items: Iterable[T]) -> Iterator[tuple[T, ...]]:
        return batched(items, cls.window_limit)

    def activity_names(self) -> frozenset[str]:
        return frozenset(name for items in self.outcomes.values()
                         for item in items for name in item.activity_names())

    @abstractmethod
    def changed(self, comms: Comms, revision: WireRevision, earlier: WireRevision) -> bool: ...


class NoNotificationWindow(NotificationWindow):
    def __init__(self) -> None:
        super().__init__({}, ())

    def changed(self, comms: Comms, revision: WireRevision, earlier: WireRevision) -> bool:
        return False
