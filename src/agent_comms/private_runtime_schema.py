"""Mandatory private coordinator declarations and runtime preparation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .coordination_errors import SchemaVersionError
from .coordination_schema import CoordinatorTable
from .typed_table import (
    DurableTables,
    SharedFileTables,
    SQLiteJournalMode,
    TypedTable,
)

if TYPE_CHECKING:
    from .coordinator import Coordination


class PrivateCoordinatorTable(SharedFileTables, DurableTables):
    """The mandatory private capabilities that share the coordinator file."""

    schema_error = SchemaVersionError

    @classmethod
    def declarations(cls) -> tuple[type[TypedTable], ...]:
        from . import (  # noqa: F401 - declarations register in the existing TypedTable family
            cohort_schema,
            coordination_response,
            native_runtime_input,
            triage_native_sources,
        )

        return TypedTable.members_with(cls)

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return cls.declarations()

    @classmethod
    def install(cls, store: Coordination) -> None:
        """Install absent current state; reject drift without repairing it."""
        cls.declarations()
        with store.session.writing() as db:
            CoordinatorTable.require_schema(db, cls.schema_rows())
            if not cls.stored_schema(db):
                cls.create_schema(db)
            cls.require_installed(db)
            if SQLiteJournalMode.read(db.execute("PRAGMA journal_mode")) != [
                SQLiteJournalMode("delete")
            ]:
                raise SchemaVersionError("private coordinator requires rollback-journal mode")
