"""Detached notification evidence and the bounded window family contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from itertools import batched
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from .audience_manifest import FrozenRecipient
    from .bus_publication import Delivered
    from .comms import Comms
    from .coordination_tables.assignments import WakeAssignment
    from .coordination_tables.harness_deliveries import HarnessDelivery
    from .field_codec import ThreadLookup
    from .message_reference import MessageReference
    from .notification_assignment import AssignmentActivityEvidence
    from .notification_progress import DeliveryProgress
    from .presentation import MessageNotification, OutcomeViewer, WireRevision
    from .thread_identity import ThreadIncarnation


@dataclass(frozen=True)
class SourceEvidence:
    """One original's captured claims and harness attempts, the attempts grouped once by
    their frozen recipient. Equality over both is the window's change test."""

    claims: tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...] = ()
    attempts: Mapping[ThreadLookup, tuple[HarnessDelivery, ...]] = field(default_factory=dict)

    def consumed_progress(self, recipient: ThreadLookup,
                          progress: DeliveryProgress) -> DeliveryProgress:
        """The recipient's progress after each of its own attempts on this original."""
        for attempt in self.attempts.get(recipient, ()):
            progress = attempt.outcome.consumed_progress(progress)
        return progress


@dataclass(frozen=True)
class NotificationAssignmentRead:
    """A window's claims and its harness attempts, both keyed by the exact original, as the
    viewer captured with them may see them: every later projection and change check of this
    read answers for that same viewer.

    A harness attempt is its own consumption evidence: a no-wake or passive recipient has no
    claim for the attempt to move, and its message still entered the session."""

    sources: tuple[Delivered, ...]
    rows: tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...]
    attempts: tuple[HarnessDelivery, ...] = ()
    viewer: OutcomeViewer = field(kw_only=True)

    @cached_property
    def evidence(self) -> Mapping[MessageReference, SourceEvidence]:
        """Each original's evidence, grouped once per captured window."""
        claims: dict[MessageReference, list[tuple[WakeAssignment, AssignmentActivityEvidence]]] = {
            source.message.reference: [] for source in self.sources
        }
        attempts: dict[MessageReference, dict[ThreadLookup, list[HarnessDelivery]]] = {
            source.message.reference: {} for source in self.sources
        }
        for row in self.rows:
            claims.setdefault(row[0].source, []).append(row)
        for attempt in self.attempts:
            attempts[attempt.reference].setdefault(attempt.recipient_lookup, []).append(attempt)
        return {source: SourceEvidence(tuple(claims[source]), {
                    lookup: tuple(rows) for lookup, rows in attempts[source].items()})
                for source in claims}

    def differs_from(self, fresh: Mapping[MessageReference, SourceEvidence]) -> bool:
        return any(previous != fresh.get(source, SourceEvidence())
                   for source, previous in self.evidence.items())


@dataclass(frozen=True)
class NotificationReadMark:
    """The exact display evidence used for a source/recipient, including no display."""

    source: Delivered
    recipient: FrozenRecipient
    displayed_to: tuple[ThreadIncarnation, ...]


@dataclass(frozen=True)
class NotificationWindow(ABC):
    """One window owns its evidence, batching and change contract."""

    window_limit: ClassVar[int] = 120
    outcomes: Mapping[tuple[int, str], tuple[MessageNotification, ...]]
    reads: tuple[NotificationAssignmentRead, ...]
    read_marks: tuple[NotificationReadMark, ...] = ()

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
