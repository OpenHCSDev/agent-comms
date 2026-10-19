"""Original refused-send outcomes in the delivery coordination database."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .active_route import guard_original_root_write
from .coordination_database import CoordinationStore
from .coordination_session import CoordinationSession
from .errors import RelationViolationError, ToolRefusedError
from .maintenance_barrier import MaintenanceBarrier
from .notification_assignment import NotificationAssignment
from .private_runtime_schema import PrivateRuntimeSchema
from .store_files import SharedLock, _store_lock
from .typed_table import Column, Index, SQLiteSchemaObject, TypedTable

if TYPE_CHECKING:
    from .coordinator import Coordination


@dataclass(frozen=True)
class SendRefusal(TypedTable, PrivateRuntimeSchema):
    """One attempted send, with no fabricated message, audience or delivery identity."""

    id: int = field(metadata={"sql": Column(primary_key=True)})
    sender: str
    target: str
    reason: str
    refused_at_ms: int
    indexes = (Index(("sender", "id")), Index(("target", "id")))

    @classmethod
    def stored_schema(cls, db: sqlite3.Connection) -> list[SQLiteSchemaObject]:
        return SQLiteSchemaObject.read(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL",
                (cls.declared_name,),
            )
        )

    @classmethod
    def require_schema(cls, actual: list[SQLiteSchemaObject]) -> None:
        if SQLiteSchemaObject.differences(actual, cls.schema_objects()):
            raise RelationViolationError(
                "Send refusal outcomes need their declared schema installed"
            )

    @classmethod
    def install(cls, store: Coordination) -> None:
        """Explicit fresh-root/upgrade operation; ordinary reads and writes never repair schema."""
        with store.session.transaction() as db:
            actual = cls.stored_schema(db)
            if actual:
                cls.require_schema(actual)
            else:
                cls.create(db)

    @classmethod
    def record(cls, root: Path, sender: str, target: str, error: ToolRefusedError) -> None:
        with guard_original_root_write(root), _store_lock(root / "wire", mode=SharedLock()):
            MaintenanceBarrier(root / "registry.json").assert_open_unlocked()
            with (
                CoordinationSession(str(NotificationAssignment.database_path(root))) as store,
                store.transaction() as db,
            ):
                cls.require_schema(cls.stored_schema(db))
                # SQLite allocates this attempt's identity. The text is exactly
                # the refusal returned to the caller, not a page's summary.
                cls.insert_values(
                    db,
                    sender=sender,
                    target=target,
                    reason=str(error),
                    refused_at_ms=time.time_ns() // 1_000_000,
                )

    @classmethod
    def recent(
        cls, root: Path, *, sender: str = "", target: str = "", limit: int = 50
    ) -> tuple[SendRefusal, ...]:
        if not 1 <= limit <= 200:
            raise ToolRefusedError("Refusal reads require a limit between 1 and 200")
        source = CoordinationStore.observed(NotificationAssignment.database_path(root))
        with source.observing(cls, sender, target, limit) as rows:
            return rows

    @classmethod
    def capture(cls, db: sqlite3.Connection, sender: str, target: str, limit: int):
        cls.require_schema(cls.stored_schema(db))
        filters = {name: value for name, value in (("sender", sender), ("target", target)) if value}
        return tuple(cls.select(db, order_by=("id",), descending=True, limit=limit, **filters))

    @classmethod
    def missing_store(cls, store, *arguments):
        return ()

    @classmethod
    def invalid_store(cls, store, *arguments):
        raise RelationViolationError(f"Send refusal outcomes unavailable: {store.failure_reason}")
