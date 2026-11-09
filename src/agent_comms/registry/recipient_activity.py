from __future__ import annotations

from dataclasses import dataclass

from agent_comms.messaging.assignment_notice import AssignmentNotice
from agent_comms.messaging.notification_contracts import RecipientActivityRead
from agent_comms.messaging.notification_progress import (
    AssignmentProgress,
    DeliveryProgress,
    TakenProgress,
)
from agent_comms.nominal.declared_family import DeclaredFamily


class RecipientActivity(
    DeclaredFamily,
    RecipientActivityRead[AssignmentNotice[DeliveryProgress], AssignmentProgress, DeliveryProgress],
    affix='RecipientActivity',
):
    """Acquired recipient availability; no assignment, start or retry authority."""

    def turn_started_by(self, updated_at_ms: int) -> bool:
        return False

    def displayed_recipient(self, notification, source, reads, document, snapshot):
        return reads.displayed_recipient(
            source, notification.recipient_identity, snapshot, document=document
        )

    def selected_response(
        self,
        notification: AssignmentNotice[DeliveryProgress],
        updated_at_ms: int,
        progress: AssignmentProgress,
    ) -> AssignmentNotice[DeliveryProgress]:
        return progress.selected_notification(
            notification, busy=self.turn_started_by(updated_at_ms)
        )

    def taken_response(
        self, notification: AssignmentNotice[DeliveryProgress], progress: DeliveryProgress,
    ) -> AssignmentNotice[DeliveryProgress]:
        """A message a running turn took, shown without a claim about current work."""
        return notification.with_state(TakenProgress(progress))


@dataclass(frozen=True)
class RecordedRecipientActivity(RecipientActivity):
    """Durable history supplies no current process, readiness or inbox check."""

    def displayed_recipient(self, notification, source, reads, document, snapshot):
        return reads.historical_displayed_recipient(
            source.message, notification.recipient_identity, snapshot, document=document
        )


@dataclass(frozen=True)
class UnavailableRecipientActivity(RecipientActivity):
    pass
