"""Mandatory private coordinator declaration discovery."""

from __future__ import annotations

from .coordination.errors import SchemaVersionError
from .storage.typed_table import (
    DurableTables,
    SharedFileTables,
    TypedTable,
)

class PrivateCoordinatorTable(SharedFileTables, DurableTables):
    """The mandatory private capabilities that share the coordinator file."""

    schema_error = SchemaVersionError

    @classmethod
    def declarations(cls) -> tuple[type[TypedTable], ...]:
        from . import cohort_schema, native_runtime_input, triage_native_sources  # noqa: F401 - declarations register in the existing TypedTable family
        from .coordination import response

        return TypedTable.members_with(cls)

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return cls.declarations()
