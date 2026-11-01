"""A captured notification window, and where a notification read settled, answer whether
their own evidence changed."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .activity import ObservedActivity
from .notification_assignment import NotificationAssignment
from agent_comms.message_notification import NoNotificationWindow, NotificationWindow
from .registry_provenance import DeletedThread
from .revision_views import ActivityView, NotificationEvidenceView

from .presentation import WireRevision
if TYPE_CHECKING:
    from .comms import Comms


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

    def live_changed(self, comms: Comms, revision: WireRevision) -> bool:
        """Read marks, participant activity or retained assignment evidence changed."""
        return (
            revision.changed_since(ActivityView(), self.revision)
            and comms.views.observed_activity(revision, self.notification_window.activity_names())
            != self.activity
        ) or self.notification_window.changed(comms, revision, self.revision)


class UnreadableNotificationsRead(NotificationsRead):
    """An unreadable read names no participants, so any later revision reads again."""

    def live_changed(self, comms: Comms, revision: WireRevision) -> bool:
        return revision != self.revision
