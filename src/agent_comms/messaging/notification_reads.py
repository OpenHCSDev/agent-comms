"""Root-free notification reads retain their original source and display cut."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from agent_comms.comms import Comms
from agent_comms.messaging.bus_read import UiRead
from agent_comms.messaging.historical_views import HistorySource
from agent_comms.messaging.message_notification import NotificationPass, NotificationWindow
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.notification_evidence import (
    FirstRecipient,
    RecipientCursor,
    RecipientLimit,
)
from agent_comms.messaging.page_traversal import HistorySourceKey
from agent_comms.messaging.read_basis import ReadAuthority
from agent_comms.notification_read import NotificationsRead


@dataclass(frozen=True)
class NotificationRead(UiRead[NotificationWindow], ABC):
    references: tuple[MessageReference, ...]
    after: RecipientCursor = field(default_factory=FirstRecipient, kw_only=True)
    limit: RecipientLimit = field(default_factory=RecipientLimit, kw_only=True)

    @abstractmethod
    def changed_since(
        self, comms: Comms, previous: NotificationsRead, notifications: NotificationPass,
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
        self, comms: Comms, previous: NotificationsRead, notifications: NotificationPass,
    ) -> bool:
        return previous.live_changed(comms, notifications)


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
        self, comms: Comms, previous: NotificationsRead, notifications: NotificationPass,
    ) -> bool:
        return notifications.revision.history != previous.revision.history
