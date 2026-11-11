"""Coordinator DDL assembled from the canonical TypedTable family."""

from __future__ import annotations

import sqlite3
from typing import Final

from agent_comms.coordination import tables
from agent_comms.coordination.errors import SchemaVersionError
from agent_comms.coordination.vocabulary import CoordinatorVocabulary
from agent_comms.storage.typed_table import DurableTables, SharedFileTables, TypedTable

COORDINATION_SNAPSHOT_VERSION: Final = 3


class CoordinationFileTable(SharedFileTables, DurableTables):
    """A durable table in the shared coordination file; a version it cannot read raises
    SchemaVersionError."""

    schema_error = SchemaVersionError


class CoordinatorTable(CoordinationFileTable):
    """The coordinator schema; cases and transitions come from their owners.

    The live objects are compared to these declarations; nothing seals a copy of them.
    """

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return tuple(cls.declarations())

    @classmethod
    def declarations(cls) -> tuple[type[TypedTable], ...]:
        return TypedTable.discover_members(tables, cls)

    @classmethod
    def configure_connection(cls, connection: sqlite3.Connection) -> None:
        """A row may install connection behavior required by its own SQL."""


class CoordinatorRow(CoordinatorTable, TypedTable, declared_name=None):
    """Coordinator rows apply their state families to the SQL they declare."""

    @classmethod
    def _sql(cls, expression: str) -> str:
        """Render state vocabulary wherever the row renders a SQL expression."""
        return super()._sql(expression).format(**CoordinatorVocabulary.schema_context())
