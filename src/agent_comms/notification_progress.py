"""One typed notification state owns its label, activity and proved progress."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredOrder
from .message_reference import MessageReference
from .thread_presentation import ThreadPresentationStatus, WorkingThreadPresentationStatus

if TYPE_CHECKING:
    from .presentation import MessageNotification


class DeliveryRank(DeclaredOrder):
    """Notification rank is selected by the first rank base in the progress MRO."""


class ActiveDelivery(DeliveryRank):
    pass


class SettledDelivery(DeliveryRank):
    pass


class WaitingDelivery(DeliveryRank):
    pass


class DeliveryProgress(ThreadPresentationStatus):
    label: ClassVar[str] = "Sent"
    detail: ClassVar[str] = ""
    delivered: ClassVar[bool]
    read: ClassVar[bool]

    def rank(self) -> int:
        return DeliveryRank.position_of(type(self))

    def notification_label(self, notification: MessageNotification) -> str:
        return self.label


class PendingDelivery:
    delivered: ClassVar[bool] = False
    read: ClassVar[bool] = False


class ReadDelivery(SettledDelivery):
    label: ClassVar[str] = "Read"
    delivered: ClassVar[bool] = True
    read: ClassVar[bool] = True


@dataclass(frozen=True)
class UnrecordedProgress(WaitingDelivery, PendingDelivery, DeliveryProgress):
    """No recipient delivery has been recorded."""


@dataclass(frozen=True)
class AcknowledgedProgress(SettledDelivery, DeliveryProgress):
    label = "Acknowledged"
    delivered: ClassVar[bool] = True
    read: ClassVar[bool] = False


@dataclass(frozen=True)
class HumanReadProgress(ReadDelivery, DeliveryProgress):
    """Human display is not model-context input."""


@dataclass(frozen=True)
class ContextualProgress(WaitingDelivery, DeliveryProgress):
    """A lifecycle label surrounds the original proof without copying its facts."""
    proof: DeliveryProgress

    @property
    def delivered(self):
        return self.proof.delivered

    @property
    def read(self):
        return self.proof.read


class PassiveProgress(ContextualProgress):
    label = "Passive"
    detail = "Available as context; no automatic response requested."


class IgnoredProgress(SettledDelivery, ContextualProgress):
    label = ReadDelivery.label

    def notification_label(self, notification):
        return self.proof.notification_label(notification)


class InterruptedProgress(ContextualProgress):
    label = "Interrupted before read"


class FailedProgress(ContextualProgress):
    label = "Failed"


class EngagedProgress(ActiveDelivery, ContextualProgress, WorkingThreadPresentationStatus):
    pass


class AnsweredProgress(SettledDelivery, ContextualProgress):
    label = "Answered"

    def notification_label(self, notification):
        return notification.timed_state(self.label, notification.answered_at_ms)


class CompletedProgress(AnsweredProgress):
    detail = "The response workflow completed."

    def notification_label(self, notification):
        return self.label

class AssignmentProgress(DeliveryProgress):
    @property
    @abstractmethod
    def at_ms(self) -> int: ...

    def notification_label(self, notification):
        return notification.timed_state(self.label, self.at_ms)

    @abstractmethod
    def selected_notification(self, notification: MessageNotification, *, busy: bool) -> MessageNotification: ...

    @abstractmethod
    def interrupted_notification(self, notification: MessageNotification) -> MessageNotification: ...

    @abstractmethod
    def read_notification(self, notification: MessageNotification) -> MessageNotification: ...

    def answered_notification(self, notification: MessageNotification,
                              reference: MessageReference, answered_at_ms: int) -> MessageNotification:
        return replace(notification, state=AnsweredProgress(self),
                       answer=reference, answered_at_ms=answered_at_ms)


@dataclass(frozen=True)
class SentProgress(WaitingDelivery, PendingDelivery, AssignmentProgress):
    sent_at_ms: int

    @property
    def at_ms(self):
        return self.sent_at_ms

    def selected_notification(self, notification, *, busy):
        return replace(notification, state=SendingProgress(self.sent_at_ms) if busy else self)

    def interrupted_notification(self, notification):
        return replace(notification, state=InterruptedProgress(self))

    def read_notification(self, notification):
        raise ValueError("Read notification has no model-context proof")


class SendingProgress(ActiveDelivery, SentProgress, WorkingThreadPresentationStatus):
    pass


@dataclass(frozen=True)
class QueuedProgress(ActiveDelivery, SentProgress):
    label = "Queued"


@dataclass(frozen=True)
class ShownToModelProgress(ReadDelivery, AssignmentProgress):
    read_at_ms: int

    @property
    def at_ms(self):
        return self.read_at_ms

    def selected_notification(self, notification, *, busy):
        state = ReadingProgress(self.read_at_ms) if busy else SelectedReadProgress(self.read_at_ms)
        return replace(notification, state=state)

    def interrupted_notification(self, notification):
        return replace(notification, state=InterruptedReadProgress(self.read_at_ms))

    def read_notification(self, notification):
        return replace(notification, state=self)


class SelectedReadProgress(ActiveDelivery, ShownToModelProgress):
    pass


class ReadingProgress(SelectedReadProgress, WorkingThreadPresentationStatus):
    pass


class InterruptedReadProgress(WaitingDelivery, ShownToModelProgress):
    pass
