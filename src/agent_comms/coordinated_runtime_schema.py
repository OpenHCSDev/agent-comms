"""Install and verify the one current declaration-derived native schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from agent_comms.coordination_errors import PublicationActivationBlocked
from agent_comms.coordinator import Coordination

from .native_runtime_input import NativeRuntimeSchemaMeta, NativeRuntimeTable
from .typed_table import SQLiteForeignKeys, SQLiteSchemaObject, TypedTable


def _schema() -> dict[str, str]:
    from . import triage_native_sources  # noqa: F401; canonical native table family

    return {
        name: sql
        for table in TypedTable.members_with(NativeRuntimeTable)
        for name, sql in table.schema_objects().items()
    }


def _digest(schema: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(schema, separators=(",", ":")).encode()).hexdigest()


def assert_native_runtime_schema(db: sqlite3.Connection) -> None:
    schema = _schema()
    try:
        meta = NativeRuntimeSchemaMeta.one(db, singleton=1)
        actual = SQLiteSchemaObject.read(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND (name LIKE 'native_runtime_%' OR name LIKE 'current_native_cursor%')"
            )
        )
    except (sqlite3.Error, ValueError, TypeError) as error:
        raise PublicationActivationBlocked("native runtime schema is not installed") from error
    if meta != NativeRuntimeSchemaMeta(singleton=1, ddl_digest=_digest(schema)):
        raise PublicationActivationBlocked("native runtime schema version differs")
    if {row.name: row.sql for row in actual} != schema or SQLiteForeignKeys.read(
        db.execute("PRAGMA foreign_keys")
    ) != [SQLiteForeignKeys(True)]:
        raise PublicationActivationBlocked("native runtime schema has drifted")


def install_native_runtime_schema(store: Coordination) -> None:
    """Explicit fresh schema install. Existing state must match the declared schema."""
    if type(store) is not Coordination:
        raise TypeError("native runtime requires the actual coordinator store")
    with store.session.transaction() as db:
        present = SQLiteSchemaObject.read(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE name=?",
                (NativeRuntimeSchemaMeta.declared_name,),
            )
        )
        if not present:
            NativeRuntimeSchemaMeta.create_schema(db)
        assert_native_runtime_schema(db)
