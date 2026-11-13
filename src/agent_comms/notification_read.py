"""A captured notification window, and where a notification read settled, answer whether
their own evidence changed."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_comms.messaging.message_notification import (
    NoNotificationWindow,
    NotificationPass,
    NotificationWindow,
)
from agent_comms.messaging.revision_views import ActivityView
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.presentation import WireRevision
from agent_comms.registry.activity import ObservedActivity
from agent_comms.registry.registry_names import DeletedThread

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True)
class NotificationsRead:
    """Where one notification read settled: its revision and the activity of the participants
    its notifications name, captured before the read for participants already known and right
    after it for new ones. The window names every watched participant; activity holds only
    the ones the registry projects, so a human or archived participant is absent from it."""

    revision: WireRevision
    activity: Mapping[str, ObservedActivity | DeletedThread] = field(default_factory=dict)
    notification_window: NotificationWindow = field(default_factory=NoNotificationWindow)

    @classmethod
    def of(
        cls,
        revision: WireRevision,
        comms: Comms,
        known: Mapping[str, ObservedActivity | DeletedThread],
        window: NotificationWindow,
    ) -> NotificationsRead:
        names = window.activity_names()
        known = {**comms.views.observed_activity(revision, names - known.keys()), **known}
        return cls(revision, {name: known[name] for name in names if name in known}, window)

    def live_changed(self, comms: Comms, notifications: NotificationPass) -> bool:
        """Read marks, participant activity or retained assignment evidence changed."""
        revision = notifications.revision
        return (
            revision.changed_since(ActivityView(), self.revision)
            and comms.views.observed_activity(revision, self.notification_window.activity_names())
            != self.activity
        ) or notifications.changed(self.notification_window, self.revision)


class StaleNotificationsRead(NotificationsRead):
    """The request's own display cut is stale: rereading it never answers, so only the UI's
    fresh request, a different family, reads again."""

    def live_changed(self, comms: Comms, notifications: NotificationPass) -> bool:
        return False


class UnreadableNotificationsRead(NotificationsRead):
    """An unreadable read names no participants, so any later revision reads again."""

    def live_changed(self, comms: Comms, notifications: NotificationPass) -> bool:
        return notifications.revision != self.revision
