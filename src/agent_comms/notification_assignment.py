"""Read-only assignment display; never executes or acknowledges work."""

from __future__ import annotations

import sqlite3
from abc import abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from itertools import batched
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, NoReturn

from .audience_manifest import FrozenRecipient
from .bus import Bus
from .coordination_database import CoordinationStore
from .coordination_tables.assignments import WakeAssignment
from .coordination_tables.executions import CurrentExecutions
from .coordination_tables.harness_deliveries import HarnessDelivery
from .coordination_tables.publications import PublicationEvidence
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, MalformedRecord, MessageId, ThreadLookup, boundary_decoder
from .message_reference import MessageReference
from .notification_progress import (
    AssignmentProgress,
    QueuedProgress,
    SentProgress,
    ShownToModelProgress,
)
from .notification_evidence import NotificationAssignmentRead, NotificationWindow, NotificationSelection
from .private_registry_guard import RegistryPrivacy
from .registry_store import RegistryStore
from .typed_table import BooleanStorage, TypedRow

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Self

    from .agent_activity import RecipientActivity
    from .bus_publication import Delivered
    from .coordination_database import InvalidStore, MissingStore
    from .presentation import MessageNotification
    from .private_registry_guard import RegistryPrivacy
    from .wake_policy import RecipientDecision


@dataclass(frozen=True)
class AssignmentContextEvidence(DeclaredFamily, affix="ContextEvidence"):
    @classmethod
    @boundary_decoder
    def from_columns(cls, committed_at_ms: object) -> AssignmentContextEvidence:
        """SQL NULL is the one foreign fact tested; a present value decodes as its own type."""
        if committed_at_ms is None:
            return NotShownContextEvidence()
        return ShownContextEvidence(FieldCodec.decode(int, committed_at_ms))

    @abstractmethod
    def progress(self, *, pending_at_ms: int, queued: bool) -> AssignmentProgress: ...


class NotShownContextEvidence(AssignmentContextEvidence):
    def progress(self, *, pending_at_ms: int, queued: bool) -> AssignmentProgress:
        progress = QueuedProgress if queued else SentProgress
        return progress(pending_at_ms)


@dataclass(frozen=True)
class ShownContextEvidence(AssignmentContextEvidence):
    """The native context commit recorded on the input row when it was proved."""

    committed_at_ms: int

    def progress(self, *, pending_at_ms: int, queued: bool) -> AssignmentProgress:
        return ShownToModelProgress(self.committed_at_ms)


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

    @abstractmethod
    def completed_notification(
        self, progress: AssignmentProgress, notification: MessageNotification
    ) -> MessageNotification: ...

    @abstractmethod
    def answered(self, notification: MessageNotification) -> MessageNotification:
        """The notification with this recorded reply attached; consumption stays its proof's."""


class UnansweredResponse(AssignmentResponse):
    def recorded(self, message: Delivered) -> AssignmentResponse:
        return AnsweredResponse(
            message.reference, int(message.timestamp * 1000)
        )

    def completed_notification(self, progress: AssignmentProgress, notification: MessageNotification) -> MessageNotification:
        return progress.consumed_notification(notification)

    def answered(self, notification: MessageNotification) -> MessageNotification:
        return notification


@dataclass(frozen=True)
class AnsweredResponse(AssignmentResponse):
    reference: MessageReference
    received_at_ms: int

    def completed_notification(self, progress: AssignmentProgress, notification: MessageNotification) -> MessageNotification:
        return progress.answered_notification(
            notification, self.reference, self.received_at_ms
        )

    def answered(self, notification: MessageNotification) -> MessageNotification:
        return notification.state.answered_notification(notification, self.reference, self.received_at_ms)


@dataclass(frozen=True)
class AssignmentActivity:
    triage_inflight: bool
    progress: AssignmentProgress
    response: AssignmentResponse

    def pending_notification(self, notification: MessageNotification, queued_at_ms: int) -> MessageNotification:
        return self.progress.selected_notification(notification, busy=False)

    def completed_notification(self, notification: MessageNotification) -> MessageNotification:
        return self.response.completed_notification(self.progress, notification)

    def answered(self, notification: MessageNotification) -> MessageNotification:
        return self.response.answered(notification)


@dataclass(frozen=True)
class AssignmentActivityEvidence(TypedRow):
    """Independent context and response facts decoded once from one SQL row."""

    triage_inflight: bool
    # Whether the recipient has a current execution: SQL answers it, no id is decoded.
    queued: bool
    context: AssignmentContextEvidence
    response: AssignmentResponse

    @classmethod
    def column_projection(cls) -> dict[str, str]:
        return {
            "triage_inflight": "triage_inflight",
            "queued": "queued",
            "context_committed_at_ms": "context_committed_at_ms",
            "response_seq": "response_seq",
            "response_message_id": "response_message_id",
            "response_received_at_ms": "response_received_at_ms",
        }

    @classmethod
    def _decode_row(cls, row, positions: Mapping[str, int]) -> AssignmentActivityEvidence:
        def column(name: str):
            return row[positions[name]]

        return cls(
            BooleanStorage.from_sql(column("triage_inflight")),
            BooleanStorage.from_sql(column("queued")),
            AssignmentContextEvidence.from_columns(column("context_committed_at_ms")),
            AssignmentResponse.from_columns(
                column("response_seq"),
                column("response_message_id"),
                column("response_received_at_ms"),
            ),
        )

    def resolve(self, *, pending_at_ms: int) -> AssignmentActivity:
        return AssignmentActivity(
            self.triage_inflight,
            self.context.progress(
                pending_at_ms=pending_at_ms,
                queued=self.queued,
            ),
            self.response,
        )

class NotificationSource(DeclaredFamily, affix="NotificationSource"):
    """Original frozen delivery or handling acknowledgement, never an execution authority."""

    @abstractmethod
    def project(self, observation: RecipientActivity,
                sender_lookup: ThreadLookup) -> MessageNotification: ...

    @abstractmethod
    def recipient(self) -> FrozenRecipient:
        """The frozen publication recipient this source answers for."""


@dataclass(frozen=True)
class UnrecordedNotificationSource(NotificationSource):
    published: FrozenRecipient
    decision: RecipientDecision

    def recipient(self) -> FrozenRecipient:
        return self.published

    def project(
        self, observation: RecipientActivity, sender_lookup: ThreadLookup
    ) -> MessageNotification:
        return self.decision.initial_notification(
            self.published, sender_lookup, observation=observation)


@dataclass(frozen=True)
class NotificationAssignment(NotificationSource):
    assignment: WakeAssignment
    activity: AssignmentActivity

    @classmethod
    def changed(cls, reads: Iterable[NotificationAssignmentRead], root: Path) -> tuple[NotificationAssignmentRead, ...]:
        """Acquire fresh bounded rows once; retained reads own their equality."""
        # A retained read owns its recipient cut. Re-reading a different viewer's
        # superset would both decode it and report a permanent false change.
        return tuple(read for read in reads
                     if read.differs_from(cls.read(root, read.selection).evidence))

    @classmethod
    def resolve(cls, read: NotificationAssignmentRead) -> tuple[NotificationAssignment, ...]:
        return tuple(cls(assignment, activity.resolve(pending_at_ms=assignment.updated_at_ms))
                     for assignment, activity in read.rows)

    @classmethod
    def for_deliveries(
        cls, selection: NotificationSelection, records: Iterable[NotificationAssignment],
    ) -> Iterator[tuple[Delivered, tuple[NotificationSource, ...]]]:
        """Match notification assignments to messages and their recorded recipients."""
        by_recipient = {}
        for record in records:
            key = (record.assignment.source, record.assignment.recipient_lookup)
            by_recipient.setdefault(key, []).append(record)
        for page in selection.pages:
            original = page.source
            reference = original.message.reference
            sources = []
            for position in page.positions:
                recipient, decision = original.recipients[position], original.decisions[position]
                matching = by_recipient.get((reference, recipient.recipient_lookup), ())
                if len(matching) > 1:
                    raise ValueError("Original notification recipient has multiple handling rows")
                sources.append(matching[0] if matching else UnrecordedNotificationSource(recipient, decision))
            yield original, tuple(sources)

    @classmethod
    def capture_responses(cls, root: Path, read: NotificationAssignmentRead) -> NotificationAssignmentRead:
        """Capture bounded independent replies after releasing the coordinator snapshot.

        A reply belongs to its original/frozen author, not to a claim lifecycle or
        model-context proof. The bus returns only the first reply per selected key,
        in sequence order, including recipients with no claim at all.
        """
        keys = frozenset(read.selection.keys)
        if not keys:
            return read
        captured = Bus(Bus.path_in(root)).recipient_replies(read.selection.keys)
        responses: dict[tuple[ThreadLookup, MessageReference], AssignmentResponse] = {}
        for record in captured:
            for reply in record.deliveries():
                for answered in reply.message.answers:
                    key = (reply.sender_lookup, answered)
                    if key in keys:
                        responses[key] = responses.get(key, UnansweredResponse()).recorded(reply.message)
        return replace(read, responses=MappingProxyType(responses))

    @classmethod
    def capture(
        cls, connection: sqlite3.Connection, selection: NotificationSelection,
        privacy: RegistryPrivacy,
    ) -> NotificationAssignmentRead:
        """Probe only selected frozen keys before joining any claim/context/proof.

        The existing (recipient_lookup, wire_seq) unique index admits each row;
        message_id corroborates the exact original rather than a current name.
        """
        if len(selection.pages) > NotificationWindow.window_limit:
            raise ValueError("Notification reads require a bounded visible message window")
        keys = tuple(dict.fromkeys(selection.keys))
        if not keys:
            return NotificationAssignmentRead(selection, ())
        privacy.coordinator_schema().require_schema(connection)
        columns = ",".join(f"w.{name}" for name in WakeAssignment.columns())
        rows = []
        batch_size = connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) // 3
        for batch in batched(keys, batch_size):
            selected = ",".join("(?,?,?)" for _ in batch)
            parameters = tuple(value for lookup, reference in batch
                               for value in (lookup, reference.seq, reference.message_id))
            rows.extend(WakeAssignment.joined(connection.execute(
                f"WITH selected(recipient_lookup,wire_seq,message_id) AS (VALUES {selected}), "
                f"w AS MATERIALIZED (SELECT original.* FROM selected "
                f"JOIN {WakeAssignment.declared_name} original "
                "ON original.recipient_lookup=selected.recipient_lookup "
                "AND original.wire_seq=selected.wire_seq AND original.message_id=selected.message_id), "
                f"context AS MATERIALIZED ({privacy.notification_context_sql(connection)}) "
                f"SELECT {columns},context.triage_inflight,"
                "pointer.execution_id IS NOT NULL AS queued,context.context_committed_at_ms,"
                "p.seq AS response_seq, p.message_id AS response_message_id, "
                "p.received_at_ms AS response_received_at_ms "
                "FROM w JOIN context ON context.assignment_id=w.assignment_id "
                f"LEFT JOIN {CurrentExecutions.declared_name} pointer "
                "ON pointer.owner_lookup=w.recipient_lookup "
                f"LEFT JOIN {PublicationEvidence.declared_name} p "
                "ON p.execution_id=w.execution_id AND p.exact_target=w.exact_target "
                "ORDER BY w.wire_seq DESC,w.recipient_lookup", parameters), AssignmentActivityEvidence))
        attempts = HarnessDelivery.for_recipients(connection, keys)
        return NotificationAssignmentRead(selection, tuple(rows), attempts)

    @classmethod
    def read(
        cls, root: Path, selection: NotificationSelection
    ) -> NotificationAssignmentRead:
        """Capture assignment evidence through the single observing connection owner."""
        privacy = RegistryStore.at(root).privacy()
        with CoordinationStore.observed(CoordinationStore.path_in(root)).observing(
            cls, selection, privacy,
        ) as result:
            captured = result
        return cls.capture_responses(root, captured)

    @classmethod
    def missing_store(
        cls, store: MissingStore, selection: NotificationSelection, privacy: RegistryPrivacy,
    ) -> NotificationAssignmentRead:
        return privacy.missing_notification_assignments(selection)

    @classmethod
    def invalid_store(
        cls, store: InvalidStore, selection: NotificationSelection, privacy: RegistryPrivacy,
    ) -> NoReturn:
        raise ValueError(f"Channel notification status unavailable: {store.failure_reason}")

    def recipient(self) -> FrozenRecipient:
        return FrozenRecipient(self.assignment.recipient_lookup, self.assignment.recipient)

    def project(
        self, observation: RecipientActivity, sender_lookup: ThreadLookup
    ) -> MessageNotification:
        return self.assignment.lifecycle.notification(
            self.recipient(),
            sender_lookup,
            observation=observation,
            activity=self.activity,
            updated_at_ms=self.assignment.updated_at_ms,
            triage_inflight=self.activity.triage_inflight,
        )
