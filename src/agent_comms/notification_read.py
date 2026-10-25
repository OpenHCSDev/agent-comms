"""A captured notification window answers whether its own evidence changed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .notification_assignment import NotificationAssignment
from .notification_evidence import NotificationWindow

if TYPE_CHECKING:
    from .comms import Comms
    from .presentation import WireRevision


@dataclass(frozen=True)
class CapturedNotificationWindow(NotificationWindow):
    def changed(self, comms: Comms, revision: WireRevision, earlier: WireRevision) -> bool:
        if revision.reads != earlier.reads and self.read_marks:
            if comms.bus.reads.changed_notifications(self.read_marks, comms.registry.snapshot()):
                return True
        return (revision.notifications != earlier.notifications
                and bool(NotificationAssignment.changed(self.reads, comms.root)))
