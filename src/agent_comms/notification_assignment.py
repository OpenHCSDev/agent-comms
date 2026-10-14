"""Read-only assignment receipt projection; never an execution or ACK authority."""

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
from .coordination_tables.publications import PublicationReceipts
from .declared_family import DeclaredFamily
from .message_reference import MessageReference
from .typed_table import SQLiteUserVersion, TypedRow

if TYPE_CHECKING:
    from .agent_activity import RecipientActivity
    from .bus_publication import CommittedDelivery
    from .presentation import MessageNotification
    from .wake import NoWakeDecision, WakeDecision


class AssignmentProgress(DeclaredFamily, affix="Progress"):
    """The original model-context receipt owns the displayed read fact."""

    @abstractmethod
    def selected_notification(
        self, notification: MessageNotification, *, busy: bool
    ) -> MessageNotification: ...

    @abstractmethod
    def interrupted_notification(self, notification: MessageNotification) -> MessageNotification: ...

    @abstractmethod
    def read_notification(self, notification: MessageNotification) -> MessageNotification: ...

    @abstractmethod
    def answered_notification(
        self, notification: MessageNotification, reference: MessageReference, answered_at_ms: int
    ) -> MessageNotification: ...


class NotShownProgress(AssignmentProgress):
    def selected_notification(self, notification, *, busy):
        return replace(
            notification,
            state="Sent",
            detail="",
            busy=busy,
            priority=notification.priority if busy else 3,
        )

    def interrupted_notification(self, notification):
        return replace(
            notification,
            state="Interrupted before read",
            detail="",
            busy=False,
            priority=3,
        )

    def read_notification(self, notification):
        raise ValueError("Read notification has no model-context receipt")

    def answered_notification(self, notification, reference, answered_at_ms):
        return replace(
            notification,
            state=notification.receipt_state("Answered", answered_at_ms),
            detail="",
            reply_reference=reference,
            answered_at_ms=answered_at_ms,
        )


@dataclass(frozen=True)
class ShownToModelProgress(AssignmentProgress):
    read_at_ms: int | None

    def selected_notification(self, notification, *, busy):
        return replace(
            notification,
            state=notification.receipt_state("Read", self.read_at_ms),
            detail="",
            busy=busy,
            read_at_ms=self.read_at_ms,
        )

    def interrupted_notification(self, notification):
        return replace(
            notification,
            state=notification.receipt_state("Read", self.read_at_ms),
            detail="",
            busy=False,
            priority=3,
            read_at_ms=self.read_at_ms,
        )

    def read_notification(self, notification):
        return replace(
            notification,
            state=notification.receipt_state("Read", self.read_at_ms),
            detail="",
            priority=2,
            busy=False,
            read_at_ms=self.read_at_ms,
        )

    def answered_notification(self, notification, reference, answered_at_ms):
        return replace(
            notification,
            state=notification.receipt_state("Answered", answered_at_ms),
            detail="",
            reply_reference=reference,
            read_at_ms=self.read_at_ms,
            answered_at_ms=answered_at_ms,
        )


class RemovedContextProgress(AssignmentProgress):
    """The proved context survives, but its recipient session was removed."""

    state = "Removed"
    detail = "Recipient session removed"

    def selected_notification(self, notification, *, busy):
        return replace(notification, state=self.state, detail=self.detail, busy=busy, priority=3)

    def interrupted_notification(self, notification):
        return replace(notification, state=self.state, detail=self.detail, busy=False, priority=3)

    def read_notification(self, notification):
        return replace(notification, state=self.state, detail=self.detail, busy=False, priority=3)

    def answered_notification(self, notification, reference, answered_at_ms):
        return replace(
            notification,
            state=notification.receipt_state("Answered", answered_at_ms),
            detail=self.detail,
            reply_reference=reference,
            answered_at_ms=answered_at_ms,
        )


@dataclass(frozen=True)
class AssignmentContext:
    """One proved native context entry; its journal owns the read time."""

    session_file: str
    session_entry_id: str

    @classmethod
    def recorded_progress(
        cls, contexts: Iterable[AssignmentContext]
    ) -> dict[AssignmentContext, AssignmentProgress]:
        from .native_entries import NativeEntry
        from .native_pi import NativeEvidenceRemoved

        grouped: dict[Path, set[AssignmentContext]] = {}
        for context in contexts:
            grouped.setdefault(Path(context.session_file), set()).add(context)
        result: dict[AssignmentContext, AssignmentProgress] = {}
        for path, selected in grouped.items():
            try:
                with NativeEntry.open_evidence(path) as evidence:
                    recorded = evidence.recorded_times(
                        {item.session_entry_id for item in selected}
                    )
            except NativeEvidenceRemoved:
                result.update((context, RemovedContextProgress()) for context in selected)
            else:
                result.update(
                    (context, ShownToModelProgress(recorded[context.session_entry_id]))
                    for context in selected
                )
        return result


@dataclass(frozen=True)
class AssignmentActivityEvidence(TypedRow):
    triage_inflight: bool
    current_execution_id: str | None
    context_session_file: str | None
    context_session_entry_id: str | None
    response_seq: int | None
    response_message_id: str | None
    response_received_at_ms: int | None

    @property
    def context(self) -> AssignmentContext | None:
        values = self.context_session_file, self.context_session_entry_id
        if values == (None, None):
            return None
        if None in values:
            raise ValueError("Recorded model-context location is incomplete")
        session_file, session_entry_id = values
        return AssignmentContext(session_file, session_entry_id)

    def resolve(self, recorded_progress: dict[AssignmentContext, AssignmentProgress]):
        context = self.context
        return AssignmentActivity(
            triage_inflight=self.triage_inflight,
            current_execution_id=self.current_execution_id,
            context_progress=(
                recorded_progress[context] if context is not None else NotShownProgress()
            ),
            response_seq=self.response_seq,
            response_message_id=self.response_message_id,
            response_received_at_ms=self.response_received_at_ms,
        )


@dataclass(frozen=True)
class AssignmentActivity:
    triage_inflight: bool
    current_execution_id: str | None
    context_progress: AssignmentProgress
    response_seq: int | None
    response_message_id: str | None
    response_received_at_ms: int | None

    @property
    def progress(self) -> AssignmentProgress:
        return self.context_progress

    def pending_notification(self, notification, queued_at_ms):
        """Project custody behind the recipient's canonical unresolved execution."""
        if self.current_execution_id is None:
            return notification
        return replace(
            notification,
            state=notification.receipt_state("Queued", queued_at_ms),
            detail="",
            busy=False,
        )

    @property
    def response_reference(self) -> MessageReference | None:
        values = self.response_seq, self.response_message_id
        if values == (None, None):
            return None
        if None in values:
            raise ValueError("Published response reference is incomplete")
        response_seq, response_message_id = values
        return MessageReference(response_seq, response_message_id)

class NotificationSource(DeclaredFamily, affix="NotificationSource"):
    """Original frozen delivery or handling receipt, never an execution authority."""

    @abstractmethod
    def project(self, observation: RecipientActivity) -> MessageNotification: ...


@dataclass(frozen=True)
class UnrecordedNotificationSource(NotificationSource):
    recipient: FrozenRecipient
    decision: WakeDecision | NoWakeDecision

    def project(self, observation: RecipientActivity):
        return self.decision.initial_notification(self.recipient, observation=observation)


@dataclass(frozen=True)
class NotificationAssignmentRead:
    """Assignment rows captured from one caller-owned coordination snapshot."""

    rows: tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...]

    def resolve(self) -> tuple[NotificationAssignment, ...]:
        contexts = {
            context for _, activity in self.rows
            if (context := activity.context) is not None
        }
        recorded_progress = AssignmentContext.recorded_progress(contexts)
        return tuple(
            NotificationAssignment(assignment, activity.resolve(recorded_progress))
            for assignment, activity in self.rows
        )


@dataclass(frozen=True)
class NotificationAssignment(NotificationSource):
    assignment: WakeAssignment
    activity: AssignmentActivity

    @classmethod
    def for_deliveries(
        cls, originals: Iterable[CommittedDelivery], records: Iterable[NotificationAssignment],
    ) -> Iterator[tuple[CommittedDelivery, tuple[NotificationSource, ...]]]:
        """Match one acquired window by its original source and recipient identities."""
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
                    raise ValueError("Original notification recipient has multiple handling receipts")
                sources.append(matching[0] if matching else UnrecordedNotificationSource(recipient, decision))
            yield original, tuple(sources)

    @classmethod
    def database_path(cls, root: Path) -> Path:
        return root / "coordination.sqlite3"

    @classmethod
    def source_paths(cls, root: Path) -> tuple[Path, ...]:
        """SQLite may publish original recipient changes only in its WAL."""
        database = cls.database_path(root)
        return database, database.with_name(database.name + "-wal")

    @classmethod
    def capture(
        cls,
        connection: sqlite3.Connection,
        predicate: str,
        parameters: tuple,
        *,
        limit: int = 0,
        ascending: bool = False,
    ) -> NotificationAssignmentRead:
        """Capture detached assignment evidence from the caller's SQLite snapshot."""
        from .coordination_tables.executions import CurrentExecutions
        from .native_runtime_input import NativeRuntimeInput

        if SQLiteUserVersion.read(connection.execute("PRAGMA user_version")) != [
            SQLiteUserVersion(COORDINATION_SCHEMA_VERSION)
        ]:
            raise ValueError("Channel notification status has an unsupported schema")
        columns = ",".join(f"w.{name}" for name in WakeAssignment.columns())
        membership = NativeRuntimeInput.source_membership_sql()
        context = (
            f"SELECT n.{{column}} FROM {NativeRuntimeInput.declared_name} n "
            f"JOIN ({membership}) member ON member.input_id=n.input_id "
            "WHERE member.assignment_id=w.assignment_id AND n.session_entry_id IS NOT NULL "
            "ORDER BY CASE n.stage WHEN 'triage' THEN 0 ELSE 1 END,member.ordinal,n.input_id LIMIT 1"
        )
        rows = WakeAssignment.joined(
            connection.execute(
                f"SELECT {columns}, EXISTS (SELECT 1 FROM {NativeRuntimeInput.declared_name} n "
                f"JOIN ({membership}) member ON member.input_id=n.input_id "
                "WHERE member.assignment_id=w.assignment_id "
                "AND n.stage='triage' AND n.session_entry_id IS NULL) "
                "AS triage_inflight, pointer.execution_id AS current_execution_id, "
                f"({context.format(column='session_file')}) AS context_session_file, "
                f"({context.format(column='session_entry_id')}) AS context_session_entry_id, "
                "p.seq AS response_seq, p.message_id AS response_message_id, "
                "p.received_at_ms AS response_received_at_ms "
                f"FROM {WakeAssignment.declared_name} w "
                f"LEFT JOIN {CurrentExecutions.declared_name} pointer "
                "ON pointer.owner_lookup=w.recipient_lookup "
                f"LEFT JOIN {PublicationReceipts.declared_name} p "
                "ON p.execution_id=w.execution_id AND p.exact_target=w.exact_target "
                f"WHERE {predicate} ORDER BY w.wire_seq {'ASC' if ascending else 'DESC'},w.recipient"
                + (" LIMIT ?" if limit else ""),
                (*parameters, limit) if limit else parameters,
            ),
            AssignmentActivityEvidence,
        )
        return NotificationAssignmentRead(tuple(rows))

    @classmethod
    def capture_window(
        cls, connection: sqlite3.Connection, sources: Sequence[CommittedDelivery]
    ) -> NotificationAssignmentRead:
        from .presentation import MessageNotification

        if len(sources) > MessageNotification.window_limit:
            raise ValueError("Notification reads require a bounded visible message window")
        if not sources:
            return NotificationAssignmentRead(())
        placeholders = ",".join("?" for _ in sources)
        return cls.capture(
            connection,
            f"w.wire_seq IN ({placeholders})",
            tuple(source.message.seq for source in sources),
        )

    @classmethod
    def select(
        cls,
        root: Path,
        predicate: str,
        parameters: tuple,
        *,
        limit: int = 0,
        ascending: bool = False,
    ):
        from .coordination_database import CoordinationStore
        from .recovery_projection import _preflight

        database = cls.database_path(root)
        failure = _preflight(database)
        if failure == "missing":
            return ()
        if failure:
            raise ValueError(f"Channel notification status unavailable: {failure}")
        with CoordinationStore.observing(database, lock_timeout=0.05) as connection:
            captured = cls.capture(
                connection, predicate, parameters, limit=limit, ascending=ascending
            )
        return captured.resolve()

    @property
    def recipient(self) -> FrozenRecipient:
        return FrozenRecipient(self.assignment.recipient_lookup, self.assignment.recipient)

    def project(self, observation: RecipientActivity) -> MessageNotification:
        return self.assignment.lifecycle.notification(
            self.recipient,
            observation=observation,
            activity=self.activity,
            updated_at_ms=self.assignment.updated_at_ms,
            triage_inflight=self.activity.triage_inflight,
        )
