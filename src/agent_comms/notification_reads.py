"""Root-free notification window operations; UI transport and MCP paging stay at their boundaries.

A notification read owns its references (and, when recorded, the archive it reads) and how
its settled evidence goes stale. The UI carries one in ``ReadNotifications`` with its own root
and request id; MCP's single-message page calls the live read directly under its caller.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .bus_read import BusReader, UiRead
from .historical_views import HistorySource
from .message_reference import MessageReference
from .notification_evidence import NotificationWindow
from .notification_read import NotificationsRead

if TYPE_CHECKING:
    from .comms import Comms
    from .presentation import WireRevision


class NotificationRead(UiRead[NotificationWindow], ABC):
    """A batched notification window read; each member owns when its settled read is stale."""

    @abstractmethod
    def changed_since(self, comms: Comms, revision: WireRevision,
                      previous: NotificationsRead) -> bool: ...


@dataclass(frozen=True)
class LiveNotificationRead(NotificationRead):
    """Recipient handling of the referenced original messages, as its reader may see them.
    The UI reads as the operator; an MCP caller reads under its own viewer."""

    references: tuple[MessageReference, ...]

    def read_as(self, comms: Comms, reader: BusReader) -> NotificationWindow:
        return comms.views.message_notifications_for_references(
            reader.outcome_viewer(comms), self.references)

    def changed_since(self, comms: Comms, revision: WireRevision,
                      previous: NotificationsRead) -> bool:
        return previous.live_changed(comms, revision)


@dataclass(frozen=True)
class RecordedNotificationRead(LiveNotificationRead):
    """The selected archive's recorded handling, independent of today's recipients."""

    source: str

    def read_as(self, comms: Comms, reader: BusReader) -> NotificationWindow:
        with comms.bus.history.read_sources() as sources:
            source = HistorySource.require(sources, self.source)
            return source.notification_references(
                sources, self.references, reader.outcome_viewer(comms))

    def changed_since(self, comms: Comms, revision: WireRevision,
                      previous: NotificationsRead) -> bool:
        return revision.history != previous.revision.history
