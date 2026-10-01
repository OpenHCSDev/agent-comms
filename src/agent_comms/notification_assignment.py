"""Read-only assignment receipt projection; never an execution or ACK authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .coordination_schema import COORDINATION_SCHEMA_VERSION
from .audience_manifest import FrozenRecipient
from .coordination_tables.assignments import WakeAssignment
from .coordination_tables.executions import CurrentExecutions
from .typed_table import SQLiteUserVersion, TypedRow

if TYPE_CHECKING:
    from .activity import Activity
    from .presentation import MessageNotification
    from .registry_document import RegistrySnapshot
    from .threads import Thread


@dataclass(frozen=True)
class AssignmentActivity(TypedRow):
    triage_inflight: bool
    current_execution_id: str | None

    def blocks(self, assignment: WakeAssignment) -> bool:
        return (
            self.current_execution_id is not None
            and self.current_execution_id != assignment.lifecycle.execution_id
        )


@dataclass(frozen=True)
class NotificationAssignment:
    assignment: WakeAssignment
    activity: AssignmentActivity

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
        import sqlite3
        from contextlib import closing

        from .native_runtime_input import NativeRuntimeInput
        from .recovery_projection import _preflight

        database = cls.database_path(root)
        failure = _preflight(database)
        if failure == "missing":
            return ()
        if failure:
            raise ValueError(f"Channel notification status unavailable: {failure}")
        with closing(
            sqlite3.connect(
                database.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.05,
                isolation_level=None,
            )
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if SQLiteUserVersion.read(connection.execute("PRAGMA user_version")) != [
                SQLiteUserVersion(COORDINATION_SCHEMA_VERSION)
            ]:
                raise ValueError("Channel notification status has an unsupported schema")
            columns = ",".join(f"w.{name}" for name in WakeAssignment.columns())
            rows = WakeAssignment.joined(
                connection.execute(
                    f"SELECT {columns}, EXISTS (SELECT 1 FROM {NativeRuntimeInput.declared_name} n "
                    "WHERE n.assignment_id=w.assignment_id "
                    "AND n.stage='triage' AND n.verdict IS NULL) "
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

    def project(
        self, owners: Mapping[str, Thread], activities: Mapping[str, Activity]
    ) -> MessageNotification:
        owner = owners.get(self.assignment.recipient_lookup)
        recipient = FrozenRecipient(self.assignment.recipient_lookup, self.assignment.recipient)
        lifecycle = self.assignment.lifecycle
        if owner is not None and (lifecycle.triage_pending or lifecycle.full_pending):
            diagnostic = activities[owner.name].diagnostic
            if diagnostic is not None:
                return diagnostic.pending_notification(recipient)
        return self.assignment.lifecycle.notification(
            recipient,
            owner_active=owner is not None,
            current_turn=(
                owner.turn_started_by(self.assignment.updated_at_ms) if owner is not None else False
            ),
            triage_inflight=self.activity.triage_inflight,
            blocked_by_prior=self.activity.blocks(self.assignment),
            prior_turn_active=owner.executing if owner is not None else False,
        )

    @staticmethod
    def active_owners(registry: RegistrySnapshot) -> Mapping[str, Thread]:
        from .bus_publication import stable_thread_lookup

        return {
            stable_thread_lookup(thread.created_at): thread
            for name, thread in registry.threads.items()
            if registry.statuses[name].active and thread.process_alive
        }
