"""One recipient's proved delivery progress, without assignment SQL dependencies."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .message_reference import MessageReference

if TYPE_CHECKING:
    from .presentation import MessageNotification


class DeliveryProgress(DeclaredFamily, affix="Progress"):
    """Delivery and read are declared contributions, never guessed from a label."""

    delivered: ClassVar[bool]
    read: ClassVar[bool]


class PendingDelivery:
    delivered: ClassVar[bool] = False
    read: ClassVar[bool] = False


class ReadDelivery:
    label: ClassVar[str] = "Read"
    delivered: ClassVar[bool] = True
    read: ClassVar[bool] = True


@dataclass(frozen=True)
class UnrecordedProgress(PendingDelivery, DeliveryProgress):
    """No recipient delivery has been recorded."""


@dataclass(frozen=True)
class AcknowledgedProgress(DeliveryProgress):
    """An external client acknowledged delivery, not a model-context read."""

    delivered: ClassVar[bool] = True
    read: ClassVar[bool] = False


@dataclass(frozen=True)
class HumanReadProgress(ReadDelivery, DeliveryProgress):
    """The human recipient displayed the message; not model-context input."""


class AssignmentProgress(DeliveryProgress):
    """A native input owns its delivery time and model-read time."""

    label: ClassVar[str]
    answered_label: ClassVar[str] = "Answered"

    @property
    @abstractmethod
    def at_ms(self) -> int: ...

    def labelled(self, notification: MessageNotification, **fields) -> MessageNotification:
        return replace(
            notification,
            state=notification.timed_state(self.label, self.at_ms),
            detail="",
            progress=self,
            **fields,
        )

    @abstractmethod
    def selected_notification(
        self, notification: MessageNotification, *, busy: bool
    ) -> MessageNotification: ...

    @abstractmethod
    def interrupted_notification(self, notification: MessageNotification) -> MessageNotification: ...

    @abstractmethod
    def read_notification(self, notification: MessageNotification) -> MessageNotification: ...

    def answered_notification(
        self, notification: MessageNotification, reference: MessageReference, answered_at_ms: int
    ) -> MessageNotification:
        return replace(
            notification,
            state=notification.timed_state(self.answered_label, answered_at_ms),
            detail="",
            answer=reference,
            answered_at_ms=answered_at_ms,
            progress=self,
        )


@dataclass(frozen=True)
class SentProgress(PendingDelivery, AssignmentProgress):
    sent_at_ms: int

    label = "Sent"
    interrupted_label: ClassVar[str] = "Interrupted before read"

    @property
    def at_ms(self):
        return self.sent_at_ms

    def selected_notification(self, notification, *, busy):
        return self.labelled(notification, busy=busy, priority=notification.priority if busy else 3)

    def interrupted_notification(self, notification):
        return replace(notification, state=self.interrupted_label, detail="", busy=False, priority=3)

    def read_notification(self, notification):
        raise ValueError("Read notification has no model-context proof")


@dataclass(frozen=True)
class QueuedProgress(SentProgress):
    label = "Queued"

    def selected_notification(self, notification, *, busy):
        return self.labelled(notification, busy=False)


@dataclass(frozen=True)
class ShownToModelProgress(ReadDelivery, AssignmentProgress):
    read_at_ms: int

    @property
    def at_ms(self):
        return self.read_at_ms

    def selected_notification(self, notification, *, busy):
        return self.labelled(notification, busy=busy)

    def interrupted_notification(self, notification):
        return self.labelled(notification, busy=False, priority=3)

    def read_notification(self, notification):
        return self.labelled(notification, busy=False, priority=2)
