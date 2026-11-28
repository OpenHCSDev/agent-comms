"""One typed notification state owns its label, activity and proved progress."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import ClassVar, Self

from agent_comms.messaging.assignment_notice import AssignmentNotice
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.message_send_reads import PublishedMessageRead
from agent_comms.nominal.declared_family import DeclaredFamily, DeclaredOrder
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord, MessageId, boundary_decoder
from agent_comms.registry.thread_presentation import (
    ThreadPresentationStatus,
    WorkingThreadPresentationStatus,
)


@dataclass(frozen=True)
class AssignmentResponse(DeclaredFamily, affix="Response"):
    def recorded(self, message: PublishedMessageRead) -> AssignmentResponse:
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
        self, progress: AssignmentProgress, notification: AssignmentNotice[DeliveryProgress]
    ) -> AssignmentNotice[DeliveryProgress]: ...

    @abstractmethod
    def answered(self, notification: AssignmentNotice[DeliveryProgress]) -> AssignmentNotice[DeliveryProgress]:
        """The notification with this recorded reply attached; consumption stays its proof's."""


class UnansweredResponse(AssignmentResponse):
    def recorded(self, message: PublishedMessageRead) -> AssignmentResponse:
        return AnsweredResponse(message.reference, message.at_ms)

    def completed_notification(
        self, progress: AssignmentProgress, notification: AssignmentNotice[DeliveryProgress]
    ) -> AssignmentNotice[DeliveryProgress]:
        return progress.consumed_notification(notification)

    def answered(self, notification: AssignmentNotice[DeliveryProgress]) -> AssignmentNotice[DeliveryProgress]:
        return notification


@dataclass(frozen=True)
class AnsweredResponse(AssignmentResponse):
    reference: MessageReference
    received_at_ms: int

    def completed_notification(
        self, progress: AssignmentProgress, notification: AssignmentNotice[DeliveryProgress]
    ) -> AssignmentNotice[DeliveryProgress]:
        return progress.answered_notification(notification, self)

    def answered(self, notification: AssignmentNotice[DeliveryProgress]) -> AssignmentNotice[DeliveryProgress]:
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


@dataclass(frozen=True, slots=True)
class DeliverySummaryCounts:
    """One recipient's contribution to the message receipt summary.

    Every recipient who is answering, answered, or not answering has read the message;
    the read count therefore includes all of them alongside plain Read receipts.
    """

    read: int = 0
    answering: int = 0
    answered: int = 0
    not_answering: int = 0

    def __add__(self, other: DeliverySummaryCounts) -> DeliverySummaryCounts:
        return DeliverySummaryCounts(
            self.read + other.read,
            self.answering + other.answering,
            self.answered + other.answered,
            self.not_answering + other.not_answering,
        )

    def as_read_only(self) -> DeliverySummaryCounts:
        return DeliverySummaryCounts(read=self.read)

    def parts(self) -> tuple[str, ...]:
        return tuple(
            f"{count} {word}"
            for count, word in (
                (self.read, "read"),
                (self.answering, "answering"),
                (self.answered, "answered"),
                (self.not_answering, "not answering"),
            )
            if count
        )


class DeliveryProgress(ThreadPresentationStatus):
    """Publication owns Delivered and the read ledger owns Read; progress answers only
    whether the recipient's model took the message into its context."""

    label: ClassVar[str] = "Sent"
    detail: ClassVar[str] = ""
    ignored_label: ClassVar[str] = "Not answering"
    def is_consumed(self) -> bool:
        return False

    def is_queued(self) -> bool:
        return False

    def shown_when_offline(self) -> bool:
        """Whether this receipt stays visible when the recipient is offline."""
        return False

    @property
    def answer(self) -> AssignmentResponse:
        return UnansweredResponse()

    def rank(self) -> int:
        return DeliveryRank.position_of(type(self))

    def contextual_notification_label(self, notification: AssignmentNotice[DeliveryProgress], label: str) -> str:
        return label

    def notification_label(self, notification: AssignmentNotice[DeliveryProgress]) -> str:
        return self.label

    @property
    def seen_not_answering(self) -> bool:
        return False

    @property
    def seen_working(self) -> bool:
        return False

    def ignored_notification_label(self, notification: AssignmentNotice[DeliveryProgress]) -> str:
        """How a recipient that chose not to answer reads, given this proof."""
        return self.ignored_label

    def working_notification_label(self, notification: AssignmentNotice[DeliveryProgress]) -> str:
        """How a busy recipient that took this message reads; without context proof, unchanged."""
        return self.notification_label(notification)

    def summary_prefix(
        self, notification: AssignmentNotice[DeliveryProgress], *, displayed: bool = False
    ) -> tuple[str, ...]:
        """What a summary led by this state prints before its counts."""
        return () if displayed else (notification.label,)

    def summary_counts(self, *, displayed: bool = False) -> DeliverySummaryCounts:
        """What read/status counts this recipient adds to the message summary."""
        return DeliverySummaryCounts(read=int(displayed))

    def answered_notification(self, notification: AssignmentNotice[DeliveryProgress],
                              response: AnsweredResponse) -> AssignmentNotice[DeliveryProgress]:
        """An independent answer preserves this state's actual context proof."""
        return replace(notification, state=AnsweredProgress(self, response), changed=True)

    def model_consumed(self, proof: ShownToModelProgress) -> DeliveryProgress:
        """The first proof that the message entered the model's context, at its own time."""
        return proof

    def ignored(self) -> DeliveryProgress:
        """A non-waking dismissal only settles a still-pending claim; other states keep their proof."""
        return self


class ConsumptionEvidence(ABC):
    """A recorded fact that may prove a delivery entered the model's context."""

    @abstractmethod
    def consumed_progress(self, progress: DeliveryProgress) -> DeliveryProgress: ...


class ReadDelivery(SettledDelivery):
    """The recipient opened or read this message; its summary is counted, never raw-prefixed."""

    def summary_prefix(
        self, notification: AssignmentNotice[DeliveryProgress], *, displayed: bool = False
    ) -> tuple[str, ...]:
        return ()

    def summary_counts(self, *, displayed: bool = False) -> DeliverySummaryCounts:
        return DeliverySummaryCounts(read=1)


class ConsumedDelivery(ReadDelivery):
    """The harness or native runtime proved the message entered the model's context."""

    # A model taking the message into its context is that recipient reading it. Calling
    # that "Consumed" told the sender which internal step fired, not whether anyone read
    # what they sent.
    label: ClassVar[str] = "Read"
    ignored_label: ClassVar[str] = "Not answering"
    def is_consumed(self) -> bool:
        return True

    def shown_when_offline(self) -> bool:
        return True

    def model_consumed(self, proof: ShownToModelProgress) -> Self:
        """An earlier proof already timed the entry; a later one adds nothing."""
        return self

    def working_notification_label(self, notification: AssignmentNotice[DeliveryProgress]) -> str:
        return WorkingTakenProgress.label


@dataclass(frozen=True)
class UnrecordedProgress(WaitingDelivery, DeliveryProgress):
    """No recipient delivery has been recorded."""

    def ignored(self) -> DeliveryProgress:
        return IgnoredProgress(self)


@dataclass(frozen=True)
class HumanReadProgress(ReadDelivery, DeliveryProgress):
    """Human display is not model-context input."""

    label: ClassVar[str] = "Read"

    def shown_when_offline(self) -> bool:
        return True


@dataclass(frozen=True)
class ContextualProgress(WaitingDelivery, DeliveryProgress):
    """A lifecycle label surrounds the original proof without copying its facts."""
    proof: DeliveryProgress

    def notification_label(self, notification: AssignmentNotice[DeliveryProgress]) -> str:
        return self.contextual_notification_label(notification, self.label)

    def contextual_notification_label(self, notification: AssignmentNotice[DeliveryProgress], label: str) -> str:
        return self.proof.contextual_notification_label(notification, label)

    def is_consumed(self) -> bool:
        return self.proof.is_consumed()

    def shown_when_offline(self) -> bool:
        return self.proof.shown_when_offline()

    @property
    def answer(self) -> AssignmentResponse:
        return self.proof.answer

    def model_consumed(self, proof: ShownToModelProgress) -> DeliveryProgress:
        return replace(self, proof=self.proof.model_consumed(proof))

    def summary_counts(self, *, displayed: bool = False) -> DeliverySummaryCounts:
        return self.proof.summary_counts(displayed=displayed).as_read_only()


class PendingProgress(ContextualProgress):
    """A pending claim adds no label of its own: once entry is proven, the proof is
    the whole state."""

    def is_queued(self) -> bool:
        return self.proof.is_queued()

    def model_consumed(self, proof: ShownToModelProgress) -> DeliveryProgress:
        return self.proof.model_consumed(proof)

    def ignored(self) -> DeliveryProgress:
        return IgnoredProgress(self.proof)


class PassiveProgress(ContextualProgress):
    label = "Passive"
    detail = "Available as context; no automatic response requested."


class IgnoredProgress(ReadDelivery, ContextualProgress):
    label = DeliveryProgress.ignored_label

    @property
    def seen_not_answering(self) -> bool:
        return self.proof.is_consumed()

    def notification_label(self, notification: AssignmentNotice[DeliveryProgress]) -> str:
        return self.proof.ignored_notification_label(notification)

    def summary_counts(self, *, displayed: bool = False) -> DeliverySummaryCounts:
        return DeliverySummaryCounts(read=1, not_answering=1)


class TakenProgress(SettledDelivery, ContextualProgress):
    """A running turn took this message as one of its inputs; its proof says how far."""

    def notification_label(self, notification: AssignmentNotice[DeliveryProgress]) -> str:
        return self.proof.notification_label(notification)

    def summary_prefix(
        self, notification: AssignmentNotice[DeliveryProgress], *, displayed: bool = False
    ) -> tuple[str, ...]:
        return self.proof.summary_prefix(notification, displayed=displayed)


class WorkingTakenProgress(ActiveDelivery, TakenProgress, WorkingThreadPresentationStatus):
    """The same taken message while its recipient is busy. Working does not promise a reply."""

    label = "Working"

    @property
    def seen_working(self) -> bool:
        return self.proof.is_consumed()

    def notification_label(self, notification: AssignmentNotice[DeliveryProgress]) -> str:
        return self.proof.working_notification_label(notification)


class InterruptedProgress(ContextualProgress):
    label = "Interrupted"


class FailedProgress(ContextualProgress):
    label = "Failed"


@dataclass(frozen=True)
class AnsweredProgress(ReadDelivery, ContextualProgress):
    response: AnsweredResponse
    label = "Answered"

    @property
    def answer(self) -> AnsweredResponse:
        return self.response

    def shown_when_offline(self) -> bool:
        return True

    def answered_notification(self, notification: AssignmentNotice[DeliveryProgress],
                              response: AnsweredResponse) -> AssignmentNotice[DeliveryProgress]:
        """A later independent response cannot replace an already recorded answer."""
        return notification

    def notification_label(self, notification: AssignmentNotice[DeliveryProgress]) -> str:
        return notification.timed_state(self.label, self.response.received_at_ms)

    def summary_counts(self, *, displayed: bool = False) -> DeliverySummaryCounts:
        return DeliverySummaryCounts(read=1, answered=1)


class CompletedProgress(ReadDelivery, ContextualProgress):
    """Workflow completion is not evidence of an independent reply."""

    label = "Completed"
    detail = "The response workflow completed."

    def shown_when_offline(self) -> bool:
        return True


class AssignmentProgress(DeliveryProgress):
    @property
    @abstractmethod
    def at_ms(self) -> int: ...

    def notification_label(self, notification: AssignmentNotice[DeliveryProgress]) -> str:
        return notification.timed_state(self.label, self.at_ms)

    @abstractmethod
    def selected_notification(self, notification: AssignmentNotice[DeliveryProgress], *, busy: bool) -> AssignmentNotice[DeliveryProgress]: ...

    @abstractmethod
    def consumed_notification(self, notification: AssignmentNotice[DeliveryProgress]) -> AssignmentNotice[DeliveryProgress]: ...


@dataclass(frozen=True)
class SentProgress(WaitingDelivery, AssignmentProgress):
    sent_at_ms: int

    @property
    def at_ms(self) -> int:
        return self.sent_at_ms

    def selected_notification(self, notification: AssignmentNotice[DeliveryProgress], *, busy: bool) -> AssignmentNotice[DeliveryProgress]:
        return replace(notification, state=SendingProgress(self.sent_at_ms) if busy else self)

    def consumed_notification(self, notification: AssignmentNotice[DeliveryProgress]) -> AssignmentNotice[DeliveryProgress]:
        raise ValueError("Consumed notification has no model-context proof")


class SendingProgress(ActiveDelivery, SentProgress, WorkingThreadPresentationStatus):
    pass


@dataclass(frozen=True)
class QueuedProgress(ActiveDelivery, SentProgress):
    label = "Queued"

    def is_queued(self) -> bool:
        return True


@dataclass(frozen=True)
class ShownToModelProgress(ConsumedDelivery, AssignmentProgress):
    consumed_at_ms: int

    def contextual_notification_label(self, notification: AssignmentNotice[DeliveryProgress], label: str) -> str:
        read = notification.timed_state(ConsumedDelivery.label, self.consumed_at_ms)
        return f"{read} · {label.lower()}"

    @property
    def at_ms(self) -> int:
        return self.consumed_at_ms

    def ignored_notification_label(self, notification: AssignmentNotice[DeliveryProgress]) -> str:
        return notification.timed_state(self.ignored_label, self.at_ms)

    def working_notification_label(self, notification: AssignmentNotice[DeliveryProgress]) -> str:
        return notification.timed_state(WorkingTakenProgress.label, self.at_ms)

    def selected_notification(self, notification: AssignmentNotice[DeliveryProgress], *, busy: bool) -> AssignmentNotice[DeliveryProgress]:
        state: ShownToModelProgress = (ConsumingProgress(self.consumed_at_ms) if busy
                 else SelectedConsumedProgress(self.consumed_at_ms))
        return replace(notification, state=state)

    def consumed_notification(self, notification: AssignmentNotice[DeliveryProgress]) -> AssignmentNotice[DeliveryProgress]:
        return replace(notification, state=self)


class HarnessConsumedProgress(ShownToModelProgress):
    """Complete original body observed in the harness transcript, at its recorded time."""


class SelectedConsumedProgress(ActiveDelivery, ShownToModelProgress):
    pass


class ConsumingProgress(SelectedConsumedProgress, WorkingThreadPresentationStatus):
    """The message is in the recipient's context and its turn is running."""

    label = "Answering"

    def summary_counts(self, *, displayed: bool = False) -> DeliverySummaryCounts:
        return DeliverySummaryCounts(read=1, answering=1)
