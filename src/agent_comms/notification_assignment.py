"""Read-only assignment receipt projection; never an execution or ACK authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .assignment_states import AssignmentState
from .coordination_schema import COORDINATION_SCHEMA_VERSION
from .coordination_tables.assignments import WakeAssignment
from .coordination_tables.executions import CurrentExecutions
from .typed_table import SQLiteUserVersion, TypedRow

if TYPE_CHECKING:
    from .presentation import MessageNotification
    from .registry_document import RegistrySnapshot
    from .threads import Thread


@dataclass(frozen=True)
class NotificationAssignment(TypedRow):
    recipient: str
    recipient_lookup: str
    wire_seq: int
    message_id: str
    lifecycle: AssignmentState
    updated_at_ms: int
    triage_inflight: bool
    current_execution_id: str | None

    @classmethod
    def select(cls, root: Path, predicate: str, parameters: tuple, *, limit: int = 0):
        import sqlite3
        from contextlib import closing

        from .native_runtime_input import NativeRuntimeInput
        from .recovery_projection import _preflight

        database = root / "coordination.sqlite3"
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
            columns = ",".join(
                f"w.{name}" for name in cls.columns() if name in WakeAssignment.columns()
            )
            return cls.read(
                connection.execute(
                    f"SELECT {columns}, EXISTS (SELECT 1 FROM {NativeRuntimeInput.declared_name} n "
                    "WHERE n.assignment_id=w.assignment_id "
                    "AND n.stage='triage' AND n.verdict IS NULL) "
                    "AS triage_inflight, c.execution_id AS current_execution_id "
                    f"FROM {WakeAssignment.declared_name} w "
                    f"LEFT JOIN {CurrentExecutions.declared_name} c "
                    "ON c.owner_lookup=w.recipient_lookup "
                    f"WHERE {predicate} ORDER BY w.wire_seq DESC,w.recipient"
                    + (" LIMIT ?" if limit else ""),
                    (*parameters, limit) if limit else parameters,
                )
            )

    def project(self, owners: Mapping[str, Thread]) -> MessageNotification:
        owner = owners.get(self.recipient_lookup)
        return self.lifecycle.notification(
            self.recipient,
            owner_active=owner is not None,
            current_turn=owner.turn_started_by(self.updated_at_ms)
            if owner is not None
            else False,
            triage_inflight=self.triage_inflight,
            blocked_by_prior=(
                self.current_execution_id is not None
                and self.current_execution_id != self.lifecycle.execution_id
            ),
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
