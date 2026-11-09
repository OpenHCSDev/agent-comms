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
        import agent_comms.cohort_schema, agent_comms.coordination.response, agent_comms.execution.native_runtime_input, agent_comms.triage_native_sources  # noqa: E401, F401 - declarations register in the existing TypedTable family

        return TypedTable.members_with(cls)

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return cls.declarations()
