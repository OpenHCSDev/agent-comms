"""Mandatory private coordinator declaration discovery."""

from __future__ import annotations

from .coordination_errors import SchemaVersionError
from .typed_table import (
    DurableTables,
    SharedFileTables,
    TypedTable,
)

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
