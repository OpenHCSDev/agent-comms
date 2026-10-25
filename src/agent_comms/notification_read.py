"""Detached evidence for one bounded notification window; no projection dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from .audience_manifest import FrozenRecipient
    from .bus_publication import Delivered
    from .coordination_tables.assignments import WakeAssignment
    from .message_reference import MessageReference
    from .notification_assignment import AssignmentActivityEvidence
    from .presentation import MessageNotification
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
class NotificationWindow:
    """Outcomes and the bounded assignment/display evidence they were projected from."""

    window_limit: ClassVar[int] = 120
    outcomes: Mapping[tuple[int, str], tuple[MessageNotification, ...]]
    reads: tuple[NotificationAssignmentRead, ...]
    read_marks: tuple[NotificationReadMark, ...] = ()
