"""Read-only assignment receipt projection; never an execution or ACK authority."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from abc import abstractmethod
from collections.abc import Iterable, Iterator
from .declared_family import DeclaredFamily

from .coordination_schema import COORDINATION_SCHEMA_VERSION
from .audience_manifest import FrozenRecipient
from .coordination_tables.assignments import WakeAssignment
from .coordination_tables.executions import CurrentExecutions
from .typed_table import SQLiteUserVersion, TypedRow

if TYPE_CHECKING:
    from .agent_activity import RecipientActivity
    from .bus_publication import CommittedDelivery
    from .wake import WakeDecision, NoWakeDecision
    from .presentation import MessageNotification


@dataclass(frozen=True)
class AssignmentActivity(TypedRow):
    triage_inflight: bool
    current_execution_id: str | None

    def blocks(self, assignment: WakeAssignment) -> bool:
        return (
            self.current_execution_id is not None
            and self.current_execution_id != assignment.lifecycle.execution_id
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
        """SQLite may publish original recipient changes only in its WAL."""
        database = cls.database_path(root)
        return database, database.with_name(database.name + "-wal")

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
            rows = WakeAssignment.joined(
                connection.execute(
                    f"SELECT {columns}, EXISTS (SELECT 1 FROM {NativeRuntimeInput.declared_name} n "
                    f"JOIN ({NativeRuntimeInput.source_membership_sql()}) member ON member.input_id=n.input_id "
                    "WHERE member.assignment_id=w.assignment_id "
                    "AND n.stage='triage' AND n.session_entry_id IS NULL) "
                    "AS triage_inflight, c.execution_id AS current_execution_id "
                    f"FROM {WakeAssignment.declared_name} w "
                    f"LEFT JOIN {CurrentExecutions.declared_name} c "
                    "ON c.owner_lookup=w.recipient_lookup "
                    f"WHERE {predicate} ORDER BY w.wire_seq {'ASC' if ascending else 'DESC'},w.recipient"
                    + (" LIMIT ?" if limit else ""),
                    (*parameters, limit) if limit else parameters,
                ),
                AssignmentActivity,
            )
            return tuple(cls(assignment, activity) for assignment, activity in rows)

    @property
    def recipient(self) -> FrozenRecipient:
        return FrozenRecipient(self.assignment.recipient_lookup, self.assignment.recipient)

    def project(self, observation: RecipientActivity) -> MessageNotification:
        return self.assignment.lifecycle.notification(
            self.recipient,
            observation=observation,
            updated_at_ms=self.assignment.updated_at_ms,
            triage_inflight=self.activity.triage_inflight,
            blocked_by_prior=self.activity.blocks(self.assignment),
        )
