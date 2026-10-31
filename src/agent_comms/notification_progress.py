"""One typed notification state owns its label, activity and proved progress."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar, Self

from .nominal.declared_family import DeclaredOrder
from .thread_presentation import ThreadPresentationStatus, WorkingThreadPresentationStatus

from .nominal.declared_family import DeclaredFamily
from .nominal.field_codec import FieldCodec, MalformedRecord, MessageId, boundary_decoder
from .message_reference import MessageReference
if TYPE_CHECKING:
    from .message_notification import MessageNotification
    from .bus_publication import Delivered


@dataclass(frozen=True)
class AssignmentResponse(DeclaredFamily, affix="Response"):
    def recorded(self, message: Delivered) -> AssignmentResponse:
        return self

    @classmethod
    @boundary_decoder
    def from_columns(
        cls, seq: object, message_id: object, received_at_ms: object
    ) -> AssignmentResponse:
        """SQL NULL is the one foreign fact tested; present values decode as their own types."""
        columns = (seq, message_id, received_at_ms)
        if all(column is None for column in columns):
            return UnansweredResponse()
        if any(column is None for column in columns):
            raise MalformedRecord("Published response evidence is incomplete")
        return AnsweredResponse(
            MessageReference(FieldCodec.decode(int, seq), FieldCodec.decode(MessageId, message_id)),
            FieldCodec.decode(int, received_at_ms),
        )

    def detail_texts(self) -> tuple[str, ...]:
        return ()

    @abstractmethod
    def completed_notification(
        self, progress: AssignmentProgress, notification: MessageNotification
    ) -> MessageNotification: ...

    @abstractmethod
    def answered(self, notification: MessageNotification) -> MessageNotification:
        """The notification with this recorded reply attached; consumption stays its proof's."""


class UnansweredResponse(AssignmentResponse):
    def recorded(self, message: Delivered) -> AssignmentResponse:
        return AnsweredResponse(message.reference, message.at_ms)

    def completed_notification(
        self, progress: AssignmentProgress, notification: MessageNotification
    ) -> MessageNotification:
        return progress.consumed_notification(notification)

    def answered(self, notification: MessageNotification) -> MessageNotification:
        return notification


@dataclass(frozen=True)
class AnsweredResponse(AssignmentResponse):
    reference: MessageReference
    received_at_ms: int

    def completed_notification(
        self, progress: AssignmentProgress, notification: MessageNotification
    ) -> MessageNotification:
        return progress.answered_notification(notification, self)

    def answered(self, notification: MessageNotification) -> MessageNotification:
        return notification.state.answered_notification(notification, self)

    def detail_texts(self) -> tuple[str, ...]:
        return (f"Answer #{self.reference.seq} · {self.reference.message_id}",)


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

    @property
    def answer(self) -> AssignmentResponse:
        return UnansweredResponse()

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

    def summary_prefix(self, notification: MessageNotification) -> tuple[str, ...]:
        """What a summary led by this state prints before its counts."""
        return (notification.label,)

    def counted_summary_prefix(self, notification: MessageNotification) -> tuple[str, ...]:
        """The lead a counted wrapper prints; a consumed proof is already named by its count."""
        return (notification.label,)

    def answered_notification(self, notification: MessageNotification,
                              response: AnsweredResponse) -> MessageNotification:
        """An independent answer preserves this state's actual context proof."""
        return replace(notification, state=AnsweredProgress(self, response), changed=True)

    def model_consumed(self, proof: ShownToModelProgress) -> DeliveryProgress:
        """The first proof that the message entered the model's context, at its own time."""
        return proof


class ConsumedDelivery(SettledDelivery):
    """The harness or native runtime proved the message entered the model's context."""

    label: ClassVar[str] = "Consumed"
    ignored_label: ClassVar[str] = "Seen, not answering"
    consumed: ClassVar[bool] = True

    def model_consumed(self, proof: ShownToModelProgress) -> Self:
        """An earlier proof already timed the entry; a later one adds nothing."""
        return self

    def working_notification_label(self, notification: MessageNotification) -> str:
        return WorkingTakenProgress.label

    def counted_summary_prefix(self, notification: MessageNotification) -> tuple[str, ...]:
        return ()


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
    def consumed(self) -> bool:
        return self.proof.consumed

    @property
    def answer(self) -> AssignmentResponse:
        return self.proof.answer

    def model_consumed(self, proof: ShownToModelProgress) -> DeliveryProgress:
        return replace(self, proof=self.proof.model_consumed(proof))


class PendingProgress(ContextualProgress):
    """A pending claim adds no label of its own: once entry is proven, the proof is
    the whole state."""

    def model_consumed(self, proof: ShownToModelProgress) -> DeliveryProgress:
        return self.proof.model_consumed(proof)


class PassiveProgress(ContextualProgress):
    label = "Passive"
    detail = "Available as context; no automatic response requested."


class IgnoredProgress(SettledDelivery, ContextualProgress):
    label = DeliveryProgress.ignored_label

    @property
    def seen_not_answering(self) -> bool:
        return self.proof.consumed

    def notification_label(self, notification: MessageNotification) -> str:
        return self.proof.ignored_notification_label(notification)

    def summary_prefix(self, notification: MessageNotification) -> tuple[str, ...]:
        return self.proof.counted_summary_prefix(notification)


class TakenProgress(SettledDelivery, ContextualProgress):
    """A running turn took this message as one of its inputs; its proof says how far."""

    def notification_label(self, notification: MessageNotification) -> str:
        return self.proof.notification_label(notification)


class WorkingTakenProgress(ActiveDelivery, TakenProgress, WorkingThreadPresentationStatus):
    """The same taken message while its recipient is busy. Working does not promise a reply."""

    label = "Seen, working"

    @property
    def seen_working(self) -> bool:
        return self.proof.consumed

    def notification_label(self, notification: MessageNotification) -> str:
        return self.proof.working_notification_label(notification)

    def summary_prefix(self, notification: MessageNotification) -> tuple[str, ...]:
        return self.proof.counted_summary_prefix(notification)


class InterruptedProgress(ContextualProgress):
    label = "Interrupted before consumed"


class FailedProgress(ContextualProgress):
    label = "Failed"


@dataclass(frozen=True)
class AnsweredProgress(SettledDelivery, ContextualProgress):
    response: AnsweredResponse
    label = "Answered"

    @property
    def answer(self) -> AnsweredResponse:
        return self.response

    def answered_notification(self, notification: MessageNotification,
                              response: AnsweredResponse) -> MessageNotification:
        """A later independent response cannot replace an already recorded answer."""
        return notification

    def notification_label(self, notification: MessageNotification) -> str:
        return notification.timed_state(self.label, self.response.received_at_ms)


class CompletedProgress(SettledDelivery, ContextualProgress):
    """Workflow completion is not evidence of an independent reply."""

    label = "Completed"
    detail = "The response workflow completed."

class AssignmentProgress(DeliveryProgress):
    @property
    @abstractmethod
    def at_ms(self) -> int: ...

    def notification_label(self, notification: MessageNotification) -> str:
        return notification.timed_state(self.label, self.at_ms)

    @abstractmethod
    def selected_notification(self, notification: MessageNotification, *, busy: bool) -> MessageNotification: ...

    @abstractmethod
    def consumed_notification(self, notification: MessageNotification) -> MessageNotification: ...


@dataclass(frozen=True)
class SentProgress(WaitingDelivery, AssignmentProgress):
    sent_at_ms: int

    @property
    def at_ms(self) -> int:
        return self.sent_at_ms

    def selected_notification(self, notification: MessageNotification, *, busy: bool) -> MessageNotification:
        return replace(notification, state=SendingProgress(self.sent_at_ms) if busy else self)

    def consumed_notification(self, notification: MessageNotification) -> MessageNotification:
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
    def at_ms(self) -> int:
        return self.consumed_at_ms

    def ignored_notification_label(self, notification: MessageNotification) -> str:
        return notification.timed_state(self.ignored_label, self.at_ms)

    def working_notification_label(self, notification: MessageNotification) -> str:
        return notification.timed_state(WorkingTakenProgress.label, self.at_ms)

    def selected_notification(self, notification: MessageNotification, *, busy: bool) -> MessageNotification:
        state: ShownToModelProgress = (ConsumingProgress(self.consumed_at_ms) if busy
                 else SelectedConsumedProgress(self.consumed_at_ms))
        return replace(notification, state=state)

    def consumed_notification(self, notification: MessageNotification) -> MessageNotification:
        return replace(notification, state=self)


class HarnessConsumedProgress(ShownToModelProgress):
    """Complete original body observed in the harness transcript, at its recorded time."""


class SelectedConsumedProgress(ActiveDelivery, ShownToModelProgress):
    pass


class ConsumingProgress(SelectedConsumedProgress, WorkingThreadPresentationStatus):
    """The message is in the recipient's context and its turn is running."""

    label = "Answering"

    @property
    def answering(self) -> bool:
        return True

    def summary_prefix(self, notification: MessageNotification) -> tuple[str, ...]:
        """Its Answering count already names it."""
        return ()
