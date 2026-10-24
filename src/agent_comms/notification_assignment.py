"""Read-only assignment display; never executes or acknowledges work."""

from __future__ import annotations

import sqlite3
from abc import abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from .audience_manifest import FrozenRecipient
from .coordination_tables.assignments import WakeAssignment
from .coordination_tables.publications import PublicationEvidence
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, MalformedRecord, MessageId
from .message_reference import MessageReference
from .notification_progress import (
    AnsweredProgress,
    AssignmentProgress,
    QueuedProgress,
    SentProgress,
    ShownToModelProgress,
)
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
    def from_columns(cls, committed_at_ms: object) -> AssignmentContextEvidence:
        committed_at_ms = FieldCodec.decode(int | None, committed_at_ms)
        if committed_at_ms is None:
            return NotShownContextEvidence()
        return ShownContextEvidence(committed_at_ms)

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
    def from_columns(
        cls, seq: object, message_id: object, received_at_ms: object
    ) -> AssignmentResponse:
        seq = FieldCodec.decode(int | None, seq)
        message_id = FieldCodec.decode(MessageId | None, message_id)
        received_at_ms = FieldCodec.decode(int | None, received_at_ms)
        if seq is None and message_id is None and received_at_ms is None:
            return UnansweredResponse()
        if seq is None or message_id is None or received_at_ms is None:
            raise MalformedRecord("Published response evidence is incomplete")
        return AnsweredResponse(MessageReference(seq, message_id), received_at_ms)

    @abstractmethod
    def completed_notification(
        self, progress: AssignmentProgress, notification: MessageNotification
    ) -> MessageNotification: ...

    @abstractmethod
    def delivered_notification(
        self, notification: MessageNotification, delivered_at_ms: int
    ) -> MessageNotification: ...


class UnansweredResponse(AssignmentResponse):
    def recorded(self, message: Delivered) -> AssignmentResponse:
        return AnsweredResponse(
            message.reference, int(message.timestamp * 1000)
        )

    def completed_notification(self, progress: AssignmentProgress, notification: MessageNotification) -> MessageNotification:
        return progress.read_notification(notification)

    def delivered_notification(self, notification: MessageNotification, delivered_at_ms: int) -> MessageNotification:
        return replace(
            notification,
            state=ShownToModelProgress(delivered_at_ms),

        )


@dataclass(frozen=True)
class AnsweredResponse(AssignmentResponse):
    reference: MessageReference
    received_at_ms: int

    def completed_notification(self, progress: AssignmentProgress, notification: MessageNotification) -> MessageNotification:
        return progress.answered_notification(
            notification, self.reference, self.received_at_ms
        )

    def delivered_notification(self, notification: MessageNotification, delivered_at_ms: int) -> MessageNotification:
        return replace(
            notification,
            state=AnsweredProgress(ShownToModelProgress(delivered_at_ms)),

            answer=self.reference,
            answered_at_ms=self.received_at_ms,
        )


@dataclass(frozen=True)
class AssignmentActivity:
    triage_inflight: bool
    progress: AssignmentProgress
    response: AssignmentResponse

    def pending_notification(self, notification: MessageNotification, queued_at_ms: int) -> MessageNotification:
        return self.progress.selected_notification(notification, busy=False)

    def completed_notification(self, notification: MessageNotification) -> MessageNotification:
        return self.response.completed_notification(self.progress, notification)

    def delivered_notification(self, notification: MessageNotification, delivered_at_ms: int) -> MessageNotification:
        return self.response.delivered_notification(notification, delivered_at_ms)

    def recorded_response(self, message: Delivered) -> Self:
        return replace(self, response=self.response.recorded(message))


@dataclass(frozen=True)
class AssignmentActivityEvidence(TypedRow):
    """Independent context and response facts decoded once from one SQL row."""

    triage_inflight: bool
    current_execution_id: str | None
    context: AssignmentContextEvidence
    response: AssignmentResponse

    @classmethod
    def column_projection(cls) -> dict[str, str]:
        return {
            "triage_inflight": "triage_inflight",
            "current_execution_id": "current_execution_id",
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
            FieldCodec.decode(str | None, column("current_execution_id")),
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
                queued=self.current_execution_id is not None,
            ),
            self.response,
        )

class NotificationSource(DeclaredFamily, affix="NotificationSource"):
    """Original frozen delivery or handling acknowledgement, never an execution authority."""

    @abstractmethod
    def project(self, observation: RecipientActivity,
                sender_lookup: str) -> MessageNotification: ...


@dataclass(frozen=True)
class UnrecordedNotificationSource(NotificationSource):
    recipient: FrozenRecipient
    decision: RecipientDecision

    def project(self, observation: RecipientActivity, sender_lookup: str) -> MessageNotification:
        return self.decision.initial_notification(
            self.recipient, sender_lookup, observation=observation)


@dataclass(frozen=True)
class NotificationAssignmentRead:
    """Assignment rows captured from one caller-owned coordination snapshot."""

    sources: tuple[Delivered, ...]
    rows: tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...]

    def resolve(self, root: Path) -> tuple[NotificationAssignment, ...]:
        resolved = tuple(
            NotificationAssignment(
                assignment, activity.resolve(pending_at_ms=assignment.updated_at_ms)
            )
            for assignment, activity in self.rows
        )
        return NotificationAssignment._recorded_conversation_responses(
            root, self.sources, resolved
        )


@dataclass(frozen=True)
class NotificationAssignment(NotificationSource):
    assignment: WakeAssignment
    activity: AssignmentActivity

    @classmethod
    def for_deliveries(
        cls, originals: Iterable[Delivered], records: Iterable[NotificationAssignment],
    ) -> Iterator[tuple[Delivered, tuple[NotificationSource, ...]]]:
        """Match notification assignments to messages and their recorded recipients."""
        by_recipient = {}
        for record in records:
            key = (record.assignment.source, record.assignment.recipient_lookup)
            by_recipient.setdefault(key, []).append(record)
        for original in originals:
            reference = original.message.reference
            sources = []
            for recipient, decision in zip(original.audience.recipients, original.decisions, strict=True):
                matching = by_recipient.get((reference, recipient.recipient_lookup), ())
                if len(matching) > 1:
                    raise ValueError("Original notification recipient has multiple handling rows")
                sources.append(matching[0] if matching else UnrecordedNotificationSource(recipient, decision))
            yield original, tuple(sources)

    @classmethod
    def _recorded_conversation_responses(
        cls, root: Path, sources: tuple[Delivered, ...],
        rows: tuple[NotificationAssignment, ...],
    ) -> tuple[NotificationAssignment, ...]:
        eligible = tuple(
            row for row in rows if row.assignment.lifecycle.records_conversation_response
        )
        if not eligible:
            return rows
        from .bus import Bus

        originals = {original.message.reference: original for original in sources}
        captured = Bus(Bus.path_in(root)).replies(
            tuple({row.assignment.source for row in eligible}))
        replies: dict[MessageReference, list[Delivered]] = {}
        for record in captured:
            for reply in record.deliveries():
                for answered in reply.message.answers:
                    replies.setdefault(answered, []).append(reply)
        resolved = []
        for row in rows:
            if row.assignment.lifecycle.records_conversation_response:
                response = row.assignment.lifecycle.recorded_response(
                    originals[row.assignment.source],
                    replies.get(row.assignment.source, ()),
                    row.assignment.recipient_lookup,
                )
                if response is not None:
                    row = replace(row, activity=row.activity.recorded_response(response))
            resolved.append(row)
        return tuple(resolved)

    @classmethod
    def capture(
        cls, connection: sqlite3.Connection, sources: Sequence[Delivered],
        privacy: RegistryPrivacy,
    ) -> NotificationAssignmentRead:
        """Capture detached assignment evidence for one bounded window of originals
        from the caller's SQLite snapshot."""
        from .presentation import MessageNotification

        sources = tuple(sources)
        if len(sources) > MessageNotification.window_limit:
            raise ValueError("Notification reads require a bounded visible message window")
        if not sources:
            return NotificationAssignmentRead((), ())
        from .coordination_tables.executions import CurrentExecutions
        privacy.coordinator_schema().require_schema(connection)
        columns = ",".join(f"w.{name}" for name in WakeAssignment.columns())
        # The window's assignments and their native-input membership are each
        # selected once; membership has no assignment index, so it is built
        # for these assignments only, not once per row and per column.
        rows = WakeAssignment.joined(
            connection.execute(
                f"WITH w AS MATERIALIZED (SELECT * FROM {WakeAssignment.declared_name} "
                f"WHERE wire_seq IN ({','.join('?' for _ in sources)})), "
                f"context AS MATERIALIZED ({privacy.notification_context_sql(connection)}) "
                f"SELECT {columns},context.triage_inflight,"
                "pointer.execution_id AS current_execution_id,context.context_committed_at_ms,"
                "p.seq AS response_seq, p.message_id AS response_message_id, "
                "p.received_at_ms AS response_received_at_ms "
                "FROM w "
                "JOIN context ON context.assignment_id=w.assignment_id "
                f"LEFT JOIN {CurrentExecutions.declared_name} pointer "
                "ON pointer.owner_lookup=w.recipient_lookup "
                f"LEFT JOIN {PublicationEvidence.declared_name} p "
                "ON p.execution_id=w.execution_id AND p.exact_target=w.exact_target "
                "ORDER BY w.wire_seq DESC,w.recipient",
                tuple(source.message.seq for source in sources),
            ),
            AssignmentActivityEvidence,
        )
        return NotificationAssignmentRead(sources, tuple(rows))

    @classmethod
    def read(
        cls, root: Path, sources: Sequence[Delivered]
    ) -> NotificationAssignmentRead:
        """Capture assignment evidence through the single observing connection owner."""
        from .coordination_database import CoordinationStore
        from .private_registry_guard import RegistryPrivacy

        privacy = RegistryPrivacy.capture(root)
        with CoordinationStore.observed(CoordinationStore.path_in(root)).observing(
            cls, sources, privacy,
        ) as result:
            return result

    @classmethod
    def missing_store(
        cls, store: MissingStore, sources: Sequence[Delivered], privacy: RegistryPrivacy,
    ) -> NotificationAssignmentRead:
        return privacy.missing_notification_assignments(sources)

    @classmethod
    def invalid_store(
        cls, store: InvalidStore, sources: Sequence[Delivered], privacy: RegistryPrivacy,
    ) -> NoReturn:
        raise ValueError(f"Channel notification status unavailable: {store.failure_reason}")

    @property
    def recipient(self) -> FrozenRecipient:
        return FrozenRecipient(self.assignment.recipient_lookup, self.assignment.recipient)

    def project(self, observation: RecipientActivity, sender_lookup: str) -> MessageNotification:
        return self.assignment.lifecycle.notification(
            self.recipient,
            sender_lookup,
            observation=observation,
            activity=self.activity,
            updated_at_ms=self.assignment.updated_at_ms,
            triage_inflight=self.activity.triage_inflight,
        )
