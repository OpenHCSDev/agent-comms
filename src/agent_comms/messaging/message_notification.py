"""One recipient's durable assignment display and the windows that hold them."""

from __future__ import annotations

from agent_comms.execution.turn_lease import TurnCompletion, TurnLeaseFence
from agent_comms.messaging.assignment_notice import AssignmentNotice
from agent_comms.messaging.message_reads import MessageDeliveryRead, MessageReferencesRead
from agent_comms.messaging.notification_contracts import NotificationEvidenceSource, RevisionChanges, RecipientActivityRead


from abc import abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cached_property
from itertools import batched
from pathlib import Path

from agent_comms.execution.transcript_sources import AssignedTranscriptSource
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import Message
from agent_comms.messaging.notification_evidence import (
    FirstRecipient,
    OutcomeViewer,
    RecipientCursor,
    RecipientLimit,
)
from agent_comms.messaging.notification_progress import (
    AssignmentProgress,
    AssignmentResponse,
    DeliveryProgress,
    HumanReadProgress,
)
from agent_comms.messaging.read_ledger import ReadDocument, ReadLedger
from agent_comms.messaging.revision_views import AssignmentEvidenceView, HumanReadEvidenceView
from agent_comms.messaging.routing import Routing
from agent_comms.nominal.field_codec import MessageId, TaggedRecordMember, TaggedUnion, ThreadLookup
from agent_comms.notification_assignment import (
    AssignmentSource,
    NotificationAssignment,
    NotificationAssignmentRead,
    NotificationReadMark,
    NotificationSelection,
)
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.agent_activity import AgentActivity
from agent_comms.registry.namespace_reads import NamespaceRead
from agent_comms.registry.recipient_activity import RecordedRecipientActivity
from agent_comms.registry.registration_reads import ActivityRegistration
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.thread_identity_source import ThreadIdentitySource
from agent_comms.registry.threads import NativeThread, Thread



@dataclass(frozen=True)
class NotificationWindow(TaggedUnion, affix=""):
    """One window owns its evidence, batching and change contract. A remote UI receives
    windows as read answers, so each record carries its kind."""


    outcomes: Mapping[tuple[int, MessageId], tuple[MessageNotification, ...]]
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
    def changed(self, notifications: NotificationPass, earlier: RevisionChanges) -> bool: ...


@dataclass(frozen=True)
class NoNotificationWindow(TaggedRecordMember, NotificationWindow):
    outcomes: Mapping[tuple[int, MessageId], tuple[MessageNotification, ...]] = field(default_factory=dict)
    reads: tuple[NotificationAssignmentRead, ...] = ()

    def changed(self, notifications: NotificationPass, earlier: RevisionChanges) -> bool:
        return False


@dataclass(frozen=True)
class CapturedNotificationWindow(TaggedRecordMember, NotificationWindow):
    def changed(self, notifications: NotificationPass, earlier: RevisionChanges) -> bool:
        return bool(
            (self.read_marks
             and notifications.revision.changed_since(HumanReadEvidenceView(), earlier)
             and notifications.evidence.changed_marks(self.read_marks))
            or (self.reads
                and notifications.revision.changed_since(AssignmentEvidenceView(), earlier)
                and NotificationAssignment.changed(self.reads, notifications.assignments))
        )


class NotificationPass:
    """One refresh pass over any number of notification windows. Each window's own earlier
    revision decides whether its evidence can have moved; the read ledger and registry are
    decoded at most once per pass, the first time a window needs them."""

    def __init__(self, comms: NotificationEvidenceSource[CapturedNotificationEvidence], revision: RevisionChanges) -> None:
        self.comms = comms
        self.revision = revision

    @cached_property
    def assignments(self) -> AssignmentSource:
        """Assignment evidence for every window this pass checks, through one source."""
        return AssignmentSource(self.comms.root)

    @cached_property
    def evidence(self) -> CapturedNotificationEvidence:
        return self.comms.notification_evidence()

    def changed(self, window: NotificationWindow, earlier: RevisionChanges) -> bool:
        return window.changed(self, earlier)


@dataclass(frozen=True)
class CapturedNotificationEvidence:
    root: Path
    ledger: ReadLedger
    document: ReadDocument
    snapshot: RegistrySnapshot

    def changed_marks(
        self, marks: Iterable[NotificationReadMark],
    ) -> tuple[NotificationReadMark, ...]:
        """Compare only the source/recipient display facts a window actually used."""
        return tuple(mark for mark in marks if mark.displayed_to != self.ledger.displayed_recipient(
            mark.source, mark.recipient, self.snapshot, document=self.document,
        ))


@dataclass(frozen=True, slots=True)
class MessageNotification(AssignmentNotice[DeliveryProgress]):
    """Read-only display of one recipient's durable assignment decision."""

    recipient_identity: FrozenRecipient
    sender_lookup: ThreadLookup
    state: DeliveryProgress
    message: Message | None = None
    displayed_to: tuple[ThreadIncarnation, ...] = ()
    changed: bool = False

    def with_state(self, state: DeliveryProgress) -> MessageNotification:
        return replace(self, state=state)

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
        """What the sender asked: that it went, how many read it, how many are answering.

        It used to answer a different question — "Passive · Observed 14 recipients
        (bounded) · Consumed 1 observed" names which internal step fired for whom, and
        leaves the reader to work out whether anybody read their message. Tristan,
        2026-11-08: "There should be read, there should be answering and there should be
        sent. I think that should be enough."

        The counts stay a bounded observation rather than the frozen audience; saying so
        in the line itself bought nothing, since a count nobody can act on is noise in the
        column where the answer should be.
        """
        ordered = cls.ordered(notifications)
        if not ordered:
            # A matched original is sent even when there is no recipient feedback
            # (including when its only synthetic row was the viewing human).
            return DeliveryProgress.label
        # A recipient has read it whether a person displayed it or a model took it into
        # context; the sender asked who read it, not which of the two paths got there.
        read = sum(1 for item in ordered
                   if item.displayed_to or item.state.is_consumed())
        counted = ((read, "read"),
                   (sum(item.state.answering for item in ordered), "answering"))
        leading = ordered[0]
        return " · ".join(
            (
                *leading.state.summary_prefix(leading),
                *(f"{count} {word}" for count, word in counted if count),
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
        registry: ActivityRegistration[Thread, NativeThread, RegistrySnapshot, StorageRule, Routing, TurnLeaseFence, TurnCompletion],
        log: MessageReferencesRead[Message],
        references: Sequence[MessageReference],
        *,
        viewer: OutcomeViewer,
        after: RecipientCursor = FirstRecipient(),
        limit: RecipientLimit = RecipientLimit(),
        snapshot: RegistrySnapshot | None = None,
    ) -> NotificationWindow:
        """Borrow mounted references in the original bounded delivery windows, as this
        viewer may see them."""
        def project(sources: Sequence[MessageDeliveryRead[Message]]) -> NotificationWindow:
            read = NotificationAssignment.read(
                root, NotificationSelection.of(viewer, sources, after=after, limit=limit)
            )
            return cls.delivery_window(root, registry, read, snapshot=snapshot)

        return cls.read_references(log, references, project)

    @classmethod
    def read_references(
        cls, log: MessageReferencesRead[Message], references: Sequence[MessageReference],
        project: Callable[[Sequence[MessageDeliveryRead[Message]]], NotificationWindow],
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
        registry: ActivityRegistration[Thread, NativeThread, RegistrySnapshot, StorageRule, Routing, TurnLeaseFence, TurnCompletion],
        assignment_read: NotificationAssignmentRead,
        *,
        snapshot: RegistrySnapshot | None = None,
    ) -> NotificationWindow:
        """The captured selection owns original membership and viewer visibility."""
        sources = assignment_read.sources
        if not sources:
            return CapturedNotificationWindow({}, (assignment_read,))
        if snapshot is None:
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
    def recorded_delivery_window[Member: ThreadIdentitySource, Selection](
        cls,
        root: Path,
        namespace: NamespaceRead[Member, Selection],
        sources: Sequence[MessageDeliveryRead[Message]],
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
    def project_delivery_window[Member: ThreadIdentitySource, Selection](
        cls,
        root: Path,
        snapshot: NamespaceRead[Member, Selection],
        observations: Mapping[ThreadLookup, RecipientActivityRead[AssignmentNotice[DeliveryProgress], AssignmentProgress, DeliveryProgress]],
        *,
        assignment_read: NotificationAssignmentRead,
    ) -> NotificationWindow:
        """Original assignment and read authorities own both display projections."""
        sources, viewer = assignment_read.sources, assignment_read.selection.viewer
        if len(sources) > NotificationSelection.window_limit:
            raise ValueError("Notification reads require a bounded visible message window")
        keys = {(source.message.seq, source.message.message_id) for source in sources}
        result: dict[tuple[int, MessageId], list[MessageNotification]] = {key: [] for key in keys}
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
                                       changed=notification.changed or state.is_consumed())
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
        cls, root: Path, registry: ActivityRegistration[Thread, NativeThread, RegistrySnapshot, StorageRule, Routing, TurnLeaseFence, TurnCompletion], log: MessageReferencesRead[Message], name: str, *, limit: int = 5
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
        registry: ActivityRegistration[Thread, NativeThread, RegistrySnapshot, StorageRule, Routing, TurnLeaseFence, TurnCompletion],
        assignment_read: NotificationAssignmentRead,
        *,
        snapshot: RegistrySnapshot | None = None,
    ) -> tuple[MessageNotification, ...]:
        """Attach notification outcomes, as the read's viewer sees them, to the public
        envelopes of the messages the caller already checked."""
        projected = cls.delivery_window(
            root,
            registry,
            assignment_read,
            snapshot=snapshot,
        )
        return tuple(
            replace(notification, message=envelope)
            for source in assignment_read.sources
            # One public envelope per message, shared by its recipients' outcomes.
            for envelope in (source.envelope,)
            for notification in projected.outcomes[source.message.seq, source.message.message_id]
        )


@dataclass(frozen=True, slots=True)
class SentMessageOutcome(MessageNotification):
    """The same typed outcome, with its original message required for context."""
    message: Message = field(kw_only=True)
