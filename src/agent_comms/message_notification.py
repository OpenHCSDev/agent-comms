"""One recipient's durable assignment display and the windows that hold them."""

from __future__ import annotations

from agent_comms.notification_assignment import NotificationSelection
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from .agent_activity import AgentActivity, RecordedRecipientActivity
from .read_ledger import ReadLedger
from .frozen_recipient import FrozenRecipient
from .channel_targets import CommandTarget
from .nominal.field_codec import ThreadLookup
from .message_reference import MessageReference
from .notification_assignment import NotificationAssignment
from agent_comms.notification_progress import AssignmentResponse
from agent_comms.notification_assignment import NotificationAssignmentRead, NotificationReadMark
from .notification_evidence import FirstRecipient, OutcomeViewer, RecipientCursor, RecipientLimit
from .notification_progress import ConsumedDelivery, ConsumingProgress, DeliveryProgress, HumanReadProgress, WorkingTakenProgress
from .thread_identity import ThreadIncarnation
from .transcript_sources import AssignedTranscriptSource
from typing import TYPE_CHECKING

from abc import ABC, abstractmethod
from collections.abc import Iterator
from itertools import batched
from typing import ClassVar
from .revision_views import NotificationEvidenceView
from .messages import Message
if TYPE_CHECKING:
    from .agent_activity import RecipientActivity
    from .bus import Bus
    from .bus_publication import Delivered
    from .registration import Registration
    from .registry_provenance import RegistryProvenance
    from .comms import Comms
    from .presentation import WireRevision


@dataclass(frozen=True)
class NotificationWindow(ABC):
    """One window owns its evidence, batching and change contract."""

    outcomes: Mapping[tuple[int, str], tuple[MessageNotification, ...]]
    reads: tuple[NotificationAssignmentRead, ...]
    read_marks: tuple[NotificationReadMark, ...] = ()

    def remaining(self, reference: MessageReference) -> int:
        return next(
            (
                page.remaining
                for read in self.reads
                for page in read.selection.pages
                if page.source.message.reference == reference
            ),
            0,
        )

    @classmethod
    def chunks[T](cls, items: Iterable[T]) -> Iterator[tuple[T, ...]]:
        return batched(items, NotificationSelection.window_limit)

    def activity_names(self) -> frozenset[str]:
        return frozenset(name for items in self.outcomes.values()
                         for item in items for name in item.activity_names())

    @abstractmethod
    def changed(self, comms: Comms, revision: WireRevision, earlier: WireRevision) -> bool: ...


class NoNotificationWindow(NotificationWindow):
    def __init__(self) -> None:
        super().__init__({}, ())

    def changed(self, comms: Comms, revision: WireRevision, earlier: WireRevision) -> bool:
        return False


@dataclass(frozen=True)
class CapturedNotificationWindow(NotificationWindow):
    def changed(self, comms: Comms, revision: WireRevision, earlier: WireRevision) -> bool:
        if not revision.changed_since(NotificationEvidenceView(), earlier):
            return False
        return bool(
            (self.read_marks and comms.bus.reads.changed_notifications(
                self.read_marks, comms.registry.snapshot(),
            )) or NotificationAssignment.changed(self.reads, comms.root)
        )


@dataclass(frozen=True, slots=True)
class MessageNotification:
    """Read-only display of one recipient's durable assignment decision."""

    recipient_identity: FrozenRecipient
    sender_lookup: ThreadLookup
    state: DeliveryProgress
    message: Message | None = None
    displayed_to: tuple[ThreadIncarnation, ...] = ()
    changed: bool = False

    @property
    def answer(self) -> AssignmentResponse:
        return self.state.answer

    @property
    def recipient(self) -> str:
        return self.recipient_identity.canonical_thread

    def activity_names(self) -> tuple[str, ...]:
        return (self.recipient, *(() if self.message is None else (self.message.sender,)))

    @property
    def label(self) -> str:
        return self.state.notification_label(self)

    def sent_outcome(self) -> SentMessageOutcome | None:
        if not self.changed or self.message is None:
            return None
        return SentMessageOutcome(
            self.recipient_identity, self.sender_lookup, self.state, message=self.message
        )

    @classmethod
    def ordered(cls, notifications: Iterable[MessageNotification]) -> tuple[MessageNotification, ...]:
        return tuple(sorted(notifications, key=lambda item: (item.state.rank(), item.recipient.casefold())))

    @classmethod
    def summary(cls, notifications: Iterable[MessageNotification]) -> str:
        """One message's state and totals; clients never classify or count agents."""
        ordered = cls.ordered(notifications)
        if not ordered:
            # A matched original is sent even when there is no recipient feedback
            # (including when its only synthetic row was the viewing human).
            return DeliveryProgress.label
        # This is a bounded observation, not the complete frozen audience and
        # not a set of runtime acknowledgements. Membership proves neither.
        total = len(ordered)
        consumed = sum(notification.state.consumed for notification in ordered)
        read = sum(bool(notification.displayed_to) for notification in ordered)
        counted = ((ConsumingProgress.label, sum(item.state.answering for item in ordered)),
                   (WorkingTakenProgress.label, sum(item.state.seen_working for item in ordered)),
                   (ConsumedDelivery.ignored_label, sum(item.state.seen_not_answering for item in ordered)))
        leading = ordered[0]
        return " · ".join(
            (
                *leading.state.summary_prefix(leading),
                *(f"{label} {count}" for label, count in counted if count),
                f"Observed {total} recipients (bounded)",
                f"{ConsumedDelivery.label} {consumed} observed",
                *((f"{HumanReadProgress.label} {read} observed",) if read else ()),
            )
        )

    @staticmethod
    def timed_state(label: str, timestamp_ms: int | None) -> str:
        """Format a recorded event in the viewer's local time."""
        if timestamp_ms is None:
            raise ValueError(f"{label} notification requires a recorded time")
        recorded = datetime.fromtimestamp(timestamp_ms / 1000).astimezone()
        now = datetime.now().astimezone()
        when = recorded.strftime("%H:%M")
        if recorded.date() != now.date():
            when = recorded.strftime("%b %d %H:%M").replace(" 0", " ")
        return f"{label} {when}"

    @classmethod
    def references(
        cls,
        root: Path,
        registry: Registration,
        log: Bus,
        references: Sequence[MessageReference],
        *,
        viewer: OutcomeViewer,
        after: RecipientCursor = FirstRecipient(),
        limit: RecipientLimit = RecipientLimit(),
    ) -> NotificationWindow:
        """Borrow mounted references in the original bounded delivery windows, as this
        viewer may see them."""
        def project(sources: Sequence[Delivered]) -> NotificationWindow:
            read = NotificationAssignment.read(
                root, NotificationSelection.of(viewer, sources, after=after, limit=limit)
            )
            return cls.delivery_window(root, registry, read)

        return cls.read_references(log, references, project)

    @classmethod
    def read_references(
        cls, log: Bus, references: Sequence[MessageReference],
        project: Callable[[Sequence[Delivered]], NotificationWindow],
    ) -> NotificationWindow:
        """Share bounded original delivery acquisition across live/recorded cuts."""
        result = {}
        reads = []
        read_marks = []
        for references_chunk in NotificationWindow.chunks(references):
            sources = log.deliveries_for_references(references_chunk)
            window = project(sources)
            result.update(window.outcomes)
            reads.extend(window.reads)
            read_marks.extend(window.read_marks)
        return CapturedNotificationWindow(result, tuple(reads), tuple(read_marks))

    @classmethod
    def delivery_window(
        cls,
        root: Path,
        registry: Registration,
        assignment_read: NotificationAssignmentRead,
    ) -> NotificationWindow:
        """The captured selection owns original membership and viewer visibility."""
        sources = assignment_read.sources
        if not sources:
            return CapturedNotificationWindow({}, (assignment_read,))
        snapshot = registry.snapshot()
        observations = AgentActivity(root, registry).observe_recipients(
            (
                (page.source, recipient)
                for page in assignment_read.selection.pages
                for recipient in page.recipients
            ),
            snapshot=snapshot,
        )
        return cls.project_delivery_window(
            root,
            snapshot,
            observations,
            assignment_read=assignment_read,
        )

    @classmethod
    def recorded_delivery_window(
        cls,
        root: Path,
        namespace: RegistryProvenance,
        sources: Sequence[Delivered],
        *,
        viewer: OutcomeViewer,
        after: RecipientCursor = FirstRecipient(),
        limit: RecipientLimit = RecipientLimit(),
    ) -> NotificationWindow:
        selection = NotificationSelection.of(viewer, sources, after=after, limit=limit)
        observations = {
            recipient.recipient_lookup: RecordedRecipientActivity()
            for page in selection.pages
            for recipient in page.recipients
        }
        read = NotificationAssignment.read(root, selection)
        return cls.project_delivery_window(
            root,
            namespace,
            observations,
            assignment_read=read,
        )

    @classmethod
    def project_delivery_window(
        cls,
        root: Path,
        snapshot: RegistryProvenance,
        observations: Mapping[ThreadLookup, RecipientActivity],
        *,
        assignment_read: NotificationAssignmentRead,
    ) -> NotificationWindow:
        """Original assignment and read authorities own both display projections."""
        sources, viewer = assignment_read.sources, assignment_read.selection.viewer
        if len(sources) > NotificationSelection.window_limit:
            raise ValueError("Notification reads require a bounded visible message window")
        keys = {(source.message.seq, source.message.message_id) for source in sources}
        result: dict[tuple[int, str], list[MessageNotification]] = {key: [] for key in keys}
        if not keys:
            return CapturedNotificationWindow({}, (assignment_read,))
        rows = NotificationAssignment.resolve(assignment_read)
        evidence = assignment_read.evidence
        read_marks = []
        reads = ReadLedger.at(root)
        document = reads.read()
        for source, outcomes in NotificationAssignment.for_deliveries(
            assignment_read.selection, rows
        ):
            key = (source.message.seq, source.message.message_id)
            for outcome in outcomes:
                recipient = outcome.recipient()
                observation = observations[recipient.recipient_lookup]
                notification = outcome.project(observation, source.sender_lookup)
                state = evidence[source.message.reference].consumed_progress(
                    recipient.recipient_lookup, notification.state)
                notification = replace(notification, state=state,
                                       changed=notification.changed or state.consumed)
                notification = evidence[source.message.reference].answered(
                    recipient.recipient_lookup, notification
                )
                displayed_to = observation.displayed_recipient(
                    notification, source, reads, document, snapshot
                )
                read_marks.append(NotificationReadMark(source, notification.recipient_identity, displayed_to))
                result[key].append(
                    replace(
                        notification,
                        displayed_to=viewer.visible_reads(displayed_to, document.bus_identity),
                        changed=notification.changed or bool(displayed_to),
                    )
                )
            direct_read = cls.direct_read_notification(
                source, snapshot, reads, document, viewer=viewer
            )
            read_marks.extend(direct_read.read_marks)
            result[key].extend(direct_read.outcomes.get(key, ()))
        return CapturedNotificationWindow({key: tuple(rows) for key, rows in result.items()},
                                  (assignment_read,), tuple(read_marks))

    @classmethod
    def direct_read_notification(
        cls, source, snapshot, reads, document, *, viewer: OutcomeViewer
    ) -> NotificationWindow:
        """Keep a direct message with no human read mark so its first display is observable."""
        message = source.message
        if source.recipients or not viewer.sees_direct_read(source):
            return CapturedNotificationWindow({}, ())
        peer = CommandTarget.named(message.target).direct_peer(snapshot)
        for target in () if peer is None else peer.role.human(peer):
            recipient = FrozenRecipient(target.incarnation.lookup, target.name)
            displayed_to = reads.displayed_recipient(
                source, recipient, snapshot, document=document
            )
            mark = NotificationReadMark(source, recipient, displayed_to)
            visible = viewer.visible_reads(displayed_to, document.bus_identity)
            notification = (
                (
                    cls(
                        recipient,
                        source.sender_lookup,
                        HumanReadProgress(),
                        displayed_to=visible,
                        changed=True,
                    ),
                )
                if visible
                else ()
            )
            return CapturedNotificationWindow({(message.seq, message.message_id): notification}, (), (mark,))
        return CapturedNotificationWindow({}, ())

    @classmethod
    def sent_sources(
        cls, root: Path, registry: Registration, log: Bus, name: str, *, limit: int = 5
    ):
        """Read up to the requested number of messages sent by this thread."""
        if not 1 <= limit <= NotificationSelection.window_limit:
            raise ValueError("Sent notification limit must fit one bounded window")
        owner = registry.require(name)

        captured = AssignedTranscriptSource.for_thread(root, owner, log).window(
            limit=0, sent_limit=limit
        )
        return owner, captured.sent_sources

    @classmethod
    def for_sources(
        cls,
        root: Path,
        registry: Registration,
        assignment_read: NotificationAssignmentRead,
    ) -> tuple[MessageNotification, ...]:
        """Attach notification outcomes, as the read's viewer sees them, to the public
        envelopes of the messages the caller already checked."""
        projected = cls.delivery_window(
            root,
            registry,
            assignment_read,
        )
        return tuple(
            replace(notification, message=source.envelope)
            for source in assignment_read.sources
            for notification in projected.outcomes[source.message.seq, source.message.message_id]
        )


@dataclass(frozen=True, slots=True)
class SentMessageOutcome(MessageNotification):
    """The same typed outcome, with its original message required for context."""
    message: Message = field(kw_only=True)
