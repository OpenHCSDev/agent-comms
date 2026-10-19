"""Read-only assignment display; never executes or acknowledges work."""

from __future__ import annotations

import sqlite3
from abc import abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .audience_manifest import FrozenRecipient
from .coordination_schema import COORDINATION_SCHEMA_VERSION
from .coordination_tables.assignments import WakeAssignment
from .coordination_tables.publications import PublicationEvidence
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, MalformedRecord
from .message_reference import MessageReference
from .notification_progress import AnsweredProgress, AssignmentProgress, QueuedProgress, SentProgress, ShownToModelProgress
from .typed_table import BooleanStorage, SQLiteUserVersion, TypedRow, sql_literal

if TYPE_CHECKING:
    from .agent_activity import RecipientActivity
    from .bus_publication import CommittedDelivery
    from .presentation import MessageNotification
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
    def progress(self, *, pending_at_ms, queued):
        progress = QueuedProgress if queued else SentProgress
        return progress(pending_at_ms)


@dataclass(frozen=True)
class ShownContextEvidence(AssignmentContextEvidence):
    """The native context commit recorded on the input row when it was proved."""

    committed_at_ms: int

    def progress(self, *, pending_at_ms, queued):
        return ShownToModelProgress(self.committed_at_ms)


@dataclass(frozen=True)
class AssignmentResponse(DeclaredFamily, affix="Response"):
    def recorded(self, message) -> AssignmentResponse:
        return self

    @classmethod
    def from_columns(
        cls, seq: object, message_id: object, received_at_ms: object
    ) -> AssignmentResponse:
        seq = FieldCodec.decode(int | None, seq)
        message_id = FieldCodec.decode(str | None, message_id)
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
    def recorded(self, message):
        return AnsweredResponse(
            message.reference, int(message.timestamp * 1000)
        )

    def completed_notification(self, progress, notification):
        return progress.read_notification(notification)

    def delivered_notification(self, notification, delivered_at_ms):
        return replace(
            notification,
            state=ShownToModelProgress(delivered_at_ms),

        )


@dataclass(frozen=True)
class AnsweredResponse(AssignmentResponse):
    reference: MessageReference
    received_at_ms: int

    def completed_notification(self, progress, notification):
        return progress.answered_notification(
            notification, self.reference, self.received_at_ms
        )

    def delivered_notification(self, notification, delivered_at_ms):
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

    def pending_notification(self, notification, queued_at_ms):
        return self.progress.selected_notification(notification, busy=False)

    def completed_notification(self, notification):
        return self.response.completed_notification(self.progress, notification)

    def delivered_notification(self, notification, delivered_at_ms):
        return self.response.delivered_notification(notification, delivered_at_ms)

    def recorded_response(self, message):
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
    def _decode_row(cls, row, positions) -> AssignmentActivityEvidence:
        def column(name):
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

    def project(self, observation: RecipientActivity, sender_lookup: str):
        return self.decision.initial_notification(
            self.recipient, sender_lookup, observation=observation)


@dataclass(frozen=True)
class NotificationAssignmentRead:
    """Assignment rows captured from one caller-owned coordination snapshot."""

    sources: tuple[CommittedDelivery, ...]
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
        cls, originals: Iterable[CommittedDelivery], records: Iterable[NotificationAssignment],
    ) -> Iterator[tuple[CommittedDelivery, tuple[NotificationSource, ...]]]:
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
    def database_path(cls, root: Path) -> Path:
        return root / "coordination.sqlite3"

    @classmethod
    def handling_paths(cls, root: Path) -> tuple[Path, ...]:
        """Handling changes belong to SQLite; wire changes have their own revision."""
        database = cls.database_path(root)
        return database, database.with_name(database.name + "-wal")

    @classmethod
    def _recorded_conversation_responses(
        cls, root: Path, sources: tuple[CommittedDelivery, ...],
        rows: tuple[NotificationAssignment, ...],
    ) -> tuple[NotificationAssignment, ...]:
        eligible = tuple(
            row for row in rows if row.assignment.lifecycle.records_conversation_response
        )
        if not eligible:
            return rows
        from .wire_log import WireLog

        originals = {original.message.reference: original for original in sources}
        with WireLog(root / "bus.jsonl").certified_read() as source:
            # Capture under the lock; decoding runs after it is released.
            captured = source.replies(tuple({row.assignment.source for row in eligible}))
        replies: dict[MessageReference, list[CommittedDelivery]] = {}
        for reply in captured:
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
        cls, connection: sqlite3.Connection, sources: Sequence[CommittedDelivery]
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
        from .native_input_record import TriageNativeExecution
        from .native_runtime_input import NativeRuntimeInput

        if SQLiteUserVersion.read(connection.execute("PRAGMA user_version")) != [
            SQLiteUserVersion(COORDINATION_SCHEMA_VERSION)
        ]:
            raise ValueError("Channel notification status has an unsupported schema")
        columns = ",".join(f"w.{name}" for name in WakeAssignment.columns())
        triage_stage = sql_literal(TriageNativeExecution)
        # The window's assignments and their native-input membership are each
        # selected once; membership has no assignment index, so it is built
        # for these assignments only, not once per row and per column.
        rows = WakeAssignment.joined(
            connection.execute(
                f"WITH w AS MATERIALIZED (SELECT * FROM {WakeAssignment.declared_name} "
                f"WHERE wire_seq IN ({','.join('?' for _ in sources)})), "
                "member AS MATERIALIZED (SELECT m.input_id,m.assignment_id,m.ordinal "
                f"FROM ({NativeRuntimeInput.source_membership_sql()}) m "
                "WHERE m.assignment_id IN (SELECT assignment_id FROM w)) "
                f"SELECT {columns}, EXISTS (SELECT 1 FROM {NativeRuntimeInput.declared_name} n "
                "JOIN member ON member.input_id=n.input_id "
                "WHERE member.assignment_id=w.assignment_id "
                f"AND n.stage={triage_stage} AND n.session_entry_id IS NULL) "
                "AS triage_inflight, pointer.execution_id AS current_execution_id, "
                f"(SELECT n.committed_at_ms FROM {NativeRuntimeInput.declared_name} n "
                "JOIN member ON member.input_id=n.input_id "
                "WHERE member.assignment_id=w.assignment_id AND n.session_entry_id IS NOT NULL "
                f"ORDER BY CASE n.stage WHEN {triage_stage} THEN 0 ELSE 1 END,"
                "member.ordinal,n.input_id LIMIT 1) AS context_committed_at_ms, "
                "p.seq AS response_seq, p.message_id AS response_message_id, "
                "p.received_at_ms AS response_received_at_ms "
                "FROM w "
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
        cls, root: Path, sources: Sequence[CommittedDelivery]
    ) -> NotificationAssignmentRead:
        """Capture assignment evidence through the single observing connection owner."""
        from .coordination_database import CoordinationStore

        with CoordinationStore.observed(cls.database_path(root)).observing(cls, sources) as result:
            return result

    @classmethod
    def missing_store(cls, store, sources):
        return NotificationAssignmentRead(tuple(sources), ())

    @classmethod
    def invalid_store(cls, store, sources):
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
