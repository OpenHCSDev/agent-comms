"""Coordinator DDL assembled from the canonical TypedTable family."""

from __future__ import annotations

import sqlite3
from typing import Final

import agent_comms.coordination_tables as coordination_tables
from agent_comms.coordination_errors import SchemaVersionError
from agent_comms.coordinator_vocabulary import CoordinatorVocabulary
from agent_comms.typed_table import DurableTables, SharedFileTables, TypedTable

COORDINATION_SNAPSHOT_VERSION: Final = 3


class CoordinatorTable(SharedFileTables, DurableTables):
    """The coordinator schema; cases and transitions come from their owners.

    The live objects are compared to these declarations; nothing seals a copy of them.
    """

    schema_error = SchemaVersionError

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return tuple(cls.declarations())

    @classmethod
    def declarations(cls) -> tuple[type[TypedTable], ...]:
        return TypedTable.discover_members(coordination_tables, cls)

    @classmethod
    def configure_connection(cls, connection: sqlite3.Connection) -> None:
        """A row may install connection behavior required by its own SQL."""


class CoordinatorRow(CoordinatorTable, TypedTable, declared_name=None):
    """Coordinator rows apply their state families to the SQL they declare."""

    @classmethod
    def schema_objects(cls, *, if_not_exists: bool = False) -> dict[str, str]:
        context = CoordinatorVocabulary.schema_context()
        return {
            name: sql.format(**context)
            for name, sql in super().schema_objects(if_not_exists=if_not_exists).items()
        }
