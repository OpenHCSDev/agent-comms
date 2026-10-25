"""Detached notification evidence and the bounded window family contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from itertools import batched
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from .audience_manifest import FrozenRecipient
    from .bus_publication import Delivered
    from .comms import Comms
    from .coordination_tables.assignments import WakeAssignment
    from .message_reference import MessageReference
    from .notification_assignment import AssignmentActivityEvidence
    from .presentation import MessageNotification, WireRevision
    from .thread_identity import ThreadIncarnation


@dataclass(frozen=True)
class NotificationAssignmentRead:
    sources: tuple[Delivered, ...]
    rows: tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...]

    @classmethod
    def empty(cls) -> NotificationAssignmentRead:
        return cls((), ())

    def rows_by_source(self) -> dict[MessageReference, tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...]]:
        grouped: dict[MessageReference, list[tuple[WakeAssignment, AssignmentActivityEvidence]]] = {
            source.message.reference: [] for source in self.sources
        }
        for row in self.rows:
            grouped.setdefault(row[0].source, []).append(row)
        return {source: tuple(rows) for source, rows in grouped.items()}

    def differs_from(self, fresh: Mapping[MessageReference, tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...]]) -> bool:
        return any(previous != fresh.get(source, ())
                   for source, previous in self.rows_by_source().items())


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
