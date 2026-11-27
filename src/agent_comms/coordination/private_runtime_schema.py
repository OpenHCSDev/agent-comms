"""Mandatory private coordinator declaration discovery."""

from __future__ import annotations

from agent_comms.coordination.errors import SchemaVersionError
from agent_comms.storage.typed_table import DurableTables, SharedFileTables, TypedTable


class PrivateCoordinatorTable(SharedFileTables, DurableTables):
    """The mandatory private capabilities that share the coordinator file."""

    schema_error = SchemaVersionError

    @classmethod
    def declarations(cls) -> tuple[type[TypedTable], ...]:
        from agent_comms.coordination import (
            cohort_schema,  # noqa: F401 - declarations register in the existing TypedTable family
        )
        from agent_comms.coordination.tables import (
            response_schema,  # noqa: F401
        )
        from agent_comms.execution import (
            native_runtime_input,  # noqa: F401
            triage_native_sources,  # noqa: F401
        )

        return TypedTable.members_with(cls)

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return cls.declarations()
