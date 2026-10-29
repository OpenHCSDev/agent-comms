"""Detached notification evidence and the bounded window family contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from itertools import batched
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, MalformedRecord, MessageId, ThreadLookup, boundary_decoder
from .message_reference import MessageReference
from .thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from .audience_manifest import FrozenRecipient
    from .read_basis import OutcomeViewer
    from .bus_publication import Delivered
    from .comms import Comms
    from .coordination_tables.assignments import WakeAssignment
    from .coordination_tables.harness_deliveries import HarnessDelivery
    from .notification_assignment import AssignmentActivityEvidence
    from .notification_progress import AssignmentProgress, DeliveryProgress
    from .presentation import MessageNotification, WireRevision


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

    @abstractmethod
    def completed_notification(
        self, progress: AssignmentProgress, notification: MessageNotification
    ) -> MessageNotification: ...

    @abstractmethod
    def answered(self, notification: MessageNotification) -> MessageNotification:
        """The notification with this recorded reply attached; consumption stays its proof's."""


class UnansweredResponse(AssignmentResponse):
    def recorded(self, message: Delivered) -> AssignmentResponse:
        return AnsweredResponse(
            message.reference, int(message.timestamp * 1000)
        )

    def completed_notification(self, progress: AssignmentProgress, notification: MessageNotification) -> MessageNotification:
        return progress.consumed_notification(notification)

    def answered(self, notification: MessageNotification) -> MessageNotification:
        return notification


@dataclass(frozen=True)
class AnsweredResponse(AssignmentResponse):
    reference: MessageReference
    received_at_ms: int

    def completed_notification(self, progress: AssignmentProgress, notification: MessageNotification) -> MessageNotification:
        return progress.answered_notification(
            notification, self.reference, self.received_at_ms
        )

    def answered(self, notification: MessageNotification) -> MessageNotification:
        return notification.state.answered_notification(notification, self.reference, self.received_at_ms)


@dataclass(frozen=True)
class SourceEvidence:
    """One original's claims, attempts and independent responses, grouped by frozen
    recipient. Equality over all three is the window's change test."""

    claims: tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...] = ()
    attempts: Mapping[ThreadLookup, tuple[HarnessDelivery, ...]] = field(default_factory=dict)
    responses: Mapping[ThreadLookup, AssignmentResponse] = field(default_factory=lambda: MappingProxyType({}))

    def answered(self, recipient: ThreadLookup, notification: MessageNotification) -> MessageNotification:
        return self.responses.get(recipient, UnansweredResponse()).answered(notification)

    def consumed_progress(self, recipient: ThreadLookup,
                          progress: DeliveryProgress) -> DeliveryProgress:
        """The recipient's progress after each of its own attempts on this original."""
        for attempt in self.attempts.get(recipient, ()):
            progress = attempt.consumed_progress(progress)
        return progress


@dataclass(frozen=True)
class RecipientSelection:
    """A bounded slice of original frozen positions, never a replacement audience."""

    source: Delivered
    eligible: range
    limit: int
    max_limit: ClassVar[int] = 100

    def __post_init__(self) -> None:
        self.require_limit(self.limit)

    @classmethod
    def require_limit(cls, limit: int) -> None:
        if not 1 <= limit <= cls.max_limit:
            raise ValueError(f"Recipient limit must be between 1 and {cls.max_limit}")

    @property
    def positions(self) -> range:
        return self.eligible[:self.limit]

    @property
    def remaining(self) -> int:
        return len(self.eligible) - len(self.positions)

    @property
    def recipients(self) -> tuple[FrozenRecipient, ...]:
        return tuple(self.source.recipients[position] for position in self.positions)


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
        return tuple((recipient.recipient_lookup, page.source.message.reference)
                     for page in self.pages for recipient in page.recipients)


@dataclass(frozen=True)
class NotificationAssignmentRead:
    """A window's claims and its harness attempts, both keyed by the exact original.

    A harness attempt is its own consumption evidence: a no-wake or passive recipient has no
    claim for the attempt to move, and its message still entered the session."""

    selection: NotificationSelection
    rows: tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...]
    attempts: tuple[HarnessDelivery, ...] = ()
    responses: Mapping[tuple[ThreadLookup, MessageReference], AssignmentResponse] = field(
        default_factory=lambda: MappingProxyType({}))

    @property
    def sources(self) -> tuple[Delivered, ...]:
        return self.selection.sources

    @cached_property
    def evidence(self) -> Mapping[MessageReference, SourceEvidence]:
        """Each original's evidence, grouped once per captured window."""
        claims: dict[MessageReference, list[tuple[WakeAssignment, AssignmentActivityEvidence]]] = {
            source.message.reference: [] for source in self.sources
        }
        attempts: dict[MessageReference, dict[ThreadLookup, list[HarnessDelivery]]] = {
            source.message.reference: {} for source in self.sources
        }
        responses: dict[MessageReference, dict[ThreadLookup, AssignmentResponse]] = {
            source.message.reference: {} for source in self.sources
        }
        for (lookup, source), response in self.responses.items():
            responses[source][lookup] = response
        for row in self.rows:
            claims.setdefault(row[0].source, []).append(row)
        for attempt in self.attempts:
            attempts[attempt.reference].setdefault(attempt.recipient_lookup, []).append(attempt)
        return {source: SourceEvidence(tuple(claims[source]), {
                    lookup: tuple(rows) for lookup, rows in attempts[source].items()},
                    MappingProxyType(responses[source]))
                for source in claims}

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
        return next((page.remaining for read in self.reads for page in read.selection.pages
                     if page.source.message.reference == reference), 0)

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
