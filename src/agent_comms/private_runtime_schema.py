"""Mandatory private coordinator declaration discovery."""

from __future__ import annotations

from agent_comms.coordination.errors import SchemaVersionError
from agent_comms.storage.typed_table import DurableTables, SharedFileTables, TypedTable


class PrivateCoordinatorTable(SharedFileTables, DurableTables):
    """The mandatory private capabilities that share the coordinator file."""

    schema_error = SchemaVersionError

    @classmethod
    def declarations(cls) -> tuple[type[TypedTable], ...]:
        import agent_comms.cohort_schema  # noqa: E401, F401 - declarations register in the existing TypedTable family
        import agent_comms.coordination.response
        import agent_comms.execution.native_runtime_input
        import agent_comms.execution.triage_native_sources

        return TypedTable.members_with(cls)

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return cls.declarations()
