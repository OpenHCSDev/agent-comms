"""Read-only assignment receipt projection; never an execution or ACK authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from abc import abstractmethod
from collections.abc import Iterable, Iterator
from .declared_family import DeclaredFamily

from .coordination_schema import COORDINATION_SCHEMA_VERSION
from .audience_manifest import FrozenRecipient
from .coordination_tables.assignments import WakeAssignment
from .coordination_tables.publications import PublicationReceipts
from .field_codec import FieldCodec
from .message_reference import MessageReference
from .typed_table import BooleanStorage, SQLiteUserVersion, TypedRow, sql_literal

if TYPE_CHECKING:
    from .agent_activity import RecipientActivity
    from .bus_publication import CommittedDelivery
    from .wake import WakeDecision, NoWakeDecision
    from .presentation import MessageNotification


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


@dataclass(frozen=True)
class SentProgress(AssignmentProgress):
    sent_at_ms: int

    def selected_notification(self, notification, *, busy):
        return replace(
            notification,
            state=notification.receipt_state("Sent", self.sent_at_ms),
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
class QueuedProgress(SentProgress):
    def selected_notification(self, notification, *, busy):
        return replace(
            notification,
            state=notification.receipt_state("Queued", self.sent_at_ms),
            detail="",
            busy=False,
        )


@dataclass(frozen=True)
class ShownToModelProgress(AssignmentProgress):
    read_at_ms: int

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


class AssignmentContextEvidence(DeclaredFamily, affix="ContextEvidence"):
    @classmethod
    def from_columns(
        cls, session_file: object, session_entry_id: object
    ) -> AssignmentContextEvidence:
        session_file = FieldCodec.decode(str | None, session_file)
        session_entry_id = FieldCodec.decode(str | None, session_entry_id)
        if session_file is None and session_entry_id is None:
            return NotShownContextEvidence()
        if session_file is None or session_entry_id is None:
            raise ValueError("Recorded model-context location is incomplete")
        return ShownContextEvidence(session_file, session_entry_id)

    @abstractmethod
    def contexts(self) -> tuple[AssignmentContext, ...]: ...

    @abstractmethod
    def progress(
        self,
        recorded: dict[AssignmentContext, AssignmentProgress],
        *,
        pending_at_ms: int,
        queued: bool,
    ) -> AssignmentProgress: ...


class NotShownContextEvidence(AssignmentContextEvidence):
    def contexts(self):
        return ()

    def progress(self, recorded, *, pending_at_ms, queued):
        progress = QueuedProgress if queued else SentProgress
        return progress(pending_at_ms)


@dataclass(frozen=True)
class ShownContextEvidence(AssignmentContextEvidence):
    session_file: str
    session_entry_id: str

    @property
    def context(self) -> AssignmentContext:
        return AssignmentContext(self.session_file, self.session_entry_id)

    def contexts(self):
        return (self.context,)

    def progress(self, recorded, *, pending_at_ms, queued):
        return recorded[self.context]


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
            raise ValueError("Published response evidence is incomplete")
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
            state=notification.receipt_state("Read", delivered_at_ms),
            detail="",
            busy=False,
            read_at_ms=delivered_at_ms,
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
            state=notification.receipt_state("Answered", self.received_at_ms),
            detail="",
            busy=False,
            reply_reference=self.reference,
            read_at_ms=delivered_at_ms,
            answered_at_ms=self.received_at_ms,
        )


@dataclass(frozen=True)
class AssignmentActivity:
    triage_inflight: bool
    context_progress: AssignmentProgress
    response: AssignmentResponse

    @property
    def progress(self) -> AssignmentProgress:
        return self.context_progress

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
    def columns(cls) -> tuple[str, ...]:
        return (
            "triage_inflight",
            "current_execution_id",
            "context_session_file",
            "context_session_entry_id",
            "response_seq",
            "response_message_id",
            "response_received_at_ms",
        )

    @classmethod
    def _decode_row(cls, row, positions) -> AssignmentActivityEvidence:
        def column(name):
            return row[positions[name]]

        return cls(
            BooleanStorage.from_sql(column("triage_inflight")),
            FieldCodec.decode(str | None, column("current_execution_id")),
            AssignmentContextEvidence.from_columns(
                column("context_session_file"), column("context_session_entry_id")
            ),
            AssignmentResponse.from_columns(
                column("response_seq"),
                column("response_message_id"),
                column("response_received_at_ms"),
            ),
        )

    def contexts(self) -> tuple[AssignmentContext, ...]:
        return self.context.contexts()

    def resolve(
        self,
        recorded: dict[AssignmentContext, AssignmentProgress],
        *,
        pending_at_ms: int,
    ) -> AssignmentActivity:
        return AssignmentActivity(
            self.triage_inflight,
            self.context.progress(
                recorded,
                pending_at_ms=pending_at_ms,
                queued=self.current_execution_id is not None,
            ),
            self.response,
        )

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
        """SQLite and the certified bus own assignment and delivered-response changes."""
        database = cls.database_path(root)
        bus = root / "bus.jsonl"
        return (
            database,
            database.with_name(database.name + "-wal"),
            bus,
            bus.with_name("bus_meta.json"),
            bus.with_name("private_bus_checkpoint.sqlite3"),
        )

    @classmethod
    def _recorded_conversation_responses(
        cls, root: Path, rows: tuple[NotificationAssignment, ...]
    ) -> tuple[NotificationAssignment, ...]:
        eligible = tuple(
            row for row in rows if row.assignment.lifecycle.records_conversation_response
        )
        if not eligible:
            return rows
        from .presentation import MessageNotification
        from .wire_log import WireLog

        bus = WireLog(root / "bus.jsonl")
        originals = {
            original.message.reference: original
            for original in bus.deliveries_for_references(
                tuple(row.assignment.source for row in eligible)
            )
        }
        candidates = {}
        for lookup in {row.assignment.recipient_lookup for row in eligible}:
            after_seq = min(
                row.assignment.wire_seq
                for row in eligible
                if row.assignment.recipient_lookup == lookup
            )
            candidates[lookup] = tuple(
                bus.conversation_sources(
                    lookup,
                    "w.seq>? AND w.sender_lookup=?",
                    (after_seq, lookup),
                    limit=MessageNotification.window_limit,
                    ascending=True,
                )
            )
        resolved = []
        for row in rows:
            original = originals.get(row.assignment.source)
            response = (
                row.assignment.lifecycle.recorded_response(
                    original,
                    candidates[row.assignment.recipient_lookup],
                    row.assignment.recipient_lookup,
                    row.assignment.updated_at_ms,
                )
                if original is not None
                else None
            )
            resolved.append(
                replace(row, activity=row.activity.recorded_response(response))
                if response is not None
                else row
            )
        return tuple(resolved)

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
        from .coordination_tables.executions import CurrentExecutions
        from .native_input_record import TriageNativeExecution
        from .native_runtime_input import NativeRuntimeInput
        from .recovery_projection import _preflight

        database = cls.database_path(root)
        failure = _preflight(database)
        if failure == "missing":
            return ()
        if failure:
            raise ValueError(f"Channel notification status unavailable: {failure}")
        with CoordinationStore.observing(database, lock_timeout=0.05) as connection:
            if SQLiteUserVersion.read(connection.execute("PRAGMA user_version")) != [
                SQLiteUserVersion(COORDINATION_SCHEMA_VERSION)
            ]:
                raise ValueError("Channel notification status has an unsupported schema")
            columns = ",".join(f"w.{name}" for name in WakeAssignment.columns())
            membership = NativeRuntimeInput.source_membership_sql()
            triage_stage = sql_literal(TriageNativeExecution)
            context = (
                f"SELECT n.{{column}} FROM {NativeRuntimeInput.declared_name} n "
                f"JOIN ({membership}) member ON member.input_id=n.input_id "
                "WHERE member.assignment_id=w.assignment_id AND n.session_entry_id IS NOT NULL "
                f"ORDER BY CASE n.stage WHEN {triage_stage} THEN 0 ELSE 1 END,"
                "member.ordinal,n.input_id LIMIT 1"
            )
            rows = WakeAssignment.joined(
                connection.execute(
                    f"SELECT {columns}, EXISTS (SELECT 1 FROM {NativeRuntimeInput.declared_name} n "
                    f"JOIN ({membership}) member ON member.input_id=n.input_id "
                    "WHERE member.assignment_id=w.assignment_id "
                    f"AND n.stage={triage_stage} AND n.session_entry_id IS NULL) "
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
        contexts = {
            context for _, activity in rows for context in activity.contexts()
        }
        recorded_progress = AssignmentContext.recorded_progress(contexts)
        resolved = tuple(
            cls(
                assignment,
                activity.resolve(
                    recorded_progress, pending_at_ms=assignment.updated_at_ms
                ),
            )
            for assignment, activity in rows
        )
        return cls._recorded_conversation_responses(root, resolved)

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
