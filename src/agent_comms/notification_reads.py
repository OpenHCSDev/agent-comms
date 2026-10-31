"""Root-free notification reads retain their original source and display cut."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .bus_read import UiRead
from .comms import Comms
from .historical_views import HistorySource, HistorySourceKey
from .message_reference import MessageReference
from agent_comms.message_notification import NotificationWindow
from .notification_evidence import FirstRecipient, RecipientCursor, RecipientLimit
from .notification_read import NotificationsRead
from .presentation import WireRevision
from .read_basis import ReadAuthority


@dataclass(frozen=True)
class NotificationRead(UiRead[NotificationWindow], ABC):
    references: tuple[MessageReference, ...]
    after: RecipientCursor = field(default_factory=FirstRecipient, kw_only=True)
    limit: RecipientLimit = field(default_factory=RecipientLimit, kw_only=True)

    @abstractmethod
    def changed_since(
        self, comms: Comms, revision: WireRevision, previous: NotificationsRead
    ) -> bool: ...


@dataclass(frozen=True)
class LiveNotificationRead(NotificationRead):
    def apply(self, comms: Comms, reader: ReadAuthority) -> NotificationWindow:
        return comms.views.message_notifications_for_references(
            reader.outcome_viewer(comms.registry),
            self.references,
            after=self.after,
            limit=self.limit,
        )

    def changed_since(
        self, comms: Comms, revision: WireRevision, previous: NotificationsRead
    ) -> bool:
        return previous.live_changed(comms, revision)


@dataclass(frozen=True)
class RecordedNotificationRead(NotificationRead):
    source: HistorySourceKey

    def apply(self, comms: Comms, reader: ReadAuthority) -> NotificationWindow:
        with comms.bus.history.read_sources() as sources:
            source = HistorySource.require(sources, self.source)
            return source.notification_references(
                sources,
                self.references,
                reader.outcome_viewer(comms.registry),
                after=self.after,
                limit=self.limit,
            )

    def changed_since(
        self, comms: Comms, revision: WireRevision, previous: NotificationsRead
    ) -> bool:
        return revision.history != previous.revision.history
