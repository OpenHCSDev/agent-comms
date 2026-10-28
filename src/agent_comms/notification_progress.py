"""One typed notification state owns its label, activity and proved progress."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar, Self

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
    """Publication owns Delivered and the read ledger owns Read; progress answers only
    whether the recipient's model took the message into its context."""

    label: ClassVar[str] = "Sent"
    detail: ClassVar[str] = ""
    ignored_label: ClassVar[str] = "Not answering"
    consumed: ClassVar[bool] = False

    def rank(self) -> int:
        return DeliveryRank.position_of(type(self))

    def notification_label(self, notification: MessageNotification) -> str:
        return self.label

    @property
    def answering(self) -> bool:
        return False

    @property
    def seen_not_answering(self) -> bool:
        return False

    @property
    def seen_working(self) -> bool:
        return False

    def ignored_notification_label(self, notification: MessageNotification) -> str:
        """How a recipient that chose not to answer reads, given this proof."""
        return self.ignored_label

    def working_notification_label(self, notification: MessageNotification) -> str:
        """How a busy recipient that took this message reads; without context proof, unchanged."""
        return self.notification_label(notification)

    def harness_consumed(self) -> DeliveryProgress:
        """This progress once a harness confirmed the message entered its session."""
        return HarnessConsumedProgress()


class ConsumedDelivery(SettledDelivery):
    """The harness or native runtime proved the message entered the model's context."""

    label: ClassVar[str] = "Consumed"
    ignored_label: ClassVar[str] = "Seen, not answering"
    consumed: ClassVar[bool] = True

    def harness_consumed(self) -> Self:
        """An earlier proof already timed the entry; a harness confirmation adds nothing."""
        return self

    def working_notification_label(self, notification):
        return WorkingTakenProgress.label


@dataclass(frozen=True)
class HarnessConsumedProgress(ConsumedDelivery, DeliveryProgress):
    """A harness confirmed context entry with no claim to time it; the attempt has no clock."""


@dataclass(frozen=True)
class UnrecordedProgress(WaitingDelivery, DeliveryProgress):
    """No recipient delivery has been recorded."""


@dataclass(frozen=True)
class HumanReadProgress(SettledDelivery, DeliveryProgress):
    """Human display is not model-context input."""

    label: ClassVar[str] = "Read"


@dataclass(frozen=True)
class ContextualProgress(WaitingDelivery, DeliveryProgress):
    """A lifecycle label surrounds the original proof without copying its facts."""
    proof: DeliveryProgress

    @property
    def consumed(self):
        return self.proof.consumed

    def harness_consumed(self) -> DeliveryProgress:
        return replace(self, proof=self.proof.harness_consumed())


class PendingProgress(ContextualProgress):
    """A pending claim adds no label of its own: once a harness proves entry, the proof is
    the whole state."""

    def harness_consumed(self) -> DeliveryProgress:
        return self.proof.harness_consumed()


class PassiveProgress(ContextualProgress):
    label = "Passive"
    detail = "Available as context; no automatic response requested."


class IgnoredProgress(SettledDelivery, ContextualProgress):
    label = DeliveryProgress.ignored_label

    @property
    def seen_not_answering(self):
        return self.proof.consumed

    def notification_label(self, notification):
        return self.proof.ignored_notification_label(notification)


class TakenProgress(ContextualProgress):
    """A running turn took this message as one of its inputs; its proof says how far."""

    def notification_label(self, notification):
        return self.proof.notification_label(notification)


class WorkingTakenProgress(ActiveDelivery, TakenProgress, WorkingThreadPresentationStatus):
    """The same taken message while its recipient is busy. Working does not promise a reply."""

    label = "Seen, working"

    @property
    def seen_working(self):
        return self.proof.consumed

    def notification_label(self, notification):
        return self.proof.working_notification_label(notification)


class InterruptedProgress(ContextualProgress):
    label = "Interrupted before consumed"


class FailedProgress(ContextualProgress):
    label = "Failed"


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
    def consumed_notification(self, notification: MessageNotification) -> MessageNotification: ...

    def answered_notification(self, notification: MessageNotification,
                              reference: MessageReference, answered_at_ms: int) -> MessageNotification:
        return replace(notification, state=AnsweredProgress(self),
                       answer=reference, answered_at_ms=answered_at_ms)


@dataclass(frozen=True)
class SentProgress(WaitingDelivery, AssignmentProgress):
    sent_at_ms: int

    @property
    def at_ms(self):
        return self.sent_at_ms

    def selected_notification(self, notification, *, busy):
        return replace(notification, state=SendingProgress(self.sent_at_ms) if busy else self)

    def consumed_notification(self, notification):
        raise ValueError("Consumed notification has no model-context proof")


class SendingProgress(ActiveDelivery, SentProgress, WorkingThreadPresentationStatus):
    pass


@dataclass(frozen=True)
class QueuedProgress(ActiveDelivery, SentProgress):
    label = "Queued"


@dataclass(frozen=True)
class ShownToModelProgress(ConsumedDelivery, AssignmentProgress):
    consumed_at_ms: int

    @property
    def at_ms(self):
        return self.consumed_at_ms

    def ignored_notification_label(self, notification):
        return notification.timed_state(self.ignored_label, self.at_ms)

    def working_notification_label(self, notification):
        return notification.timed_state(WorkingTakenProgress.label, self.at_ms)

    def selected_notification(self, notification, *, busy):
        state = (ConsumingProgress(self.consumed_at_ms) if busy
                 else SelectedConsumedProgress(self.consumed_at_ms))
        return replace(notification, state=state)

    def consumed_notification(self, notification):
        return replace(notification, state=self)


class SelectedConsumedProgress(ActiveDelivery, ShownToModelProgress):
    pass


class ConsumingProgress(SelectedConsumedProgress, WorkingThreadPresentationStatus):
    """The message is in the recipient's context and its turn is running."""

    label = "Answering"

    @property
    def answering(self):
        return True
