"""Install and verify the one current declaration-derived native schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from .coordination_store import MutationStore, PublicationActivationBlocked
from .native_runtime_input import NativeRuntimeSchemaMeta, NativeRuntimeTable
from .typed_table import SQLiteForeignKeys, SQLiteSchemaObject, TypedTable


def _schema() -> dict[str, str]:
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
    if meta != NativeRuntimeSchemaMeta(1, 4, _digest(schema)):
        raise PublicationActivationBlocked("native runtime schema version differs")
    if {row.name: row.sql for row in actual} != schema or SQLiteForeignKeys.read(
        db.execute("PRAGMA foreign_keys")
    ) != [SQLiteForeignKeys(True)]:
        raise PublicationActivationBlocked("native runtime schema has drifted")


def install_native_runtime_schema(store: MutationStore) -> None:
    """Explicit fresh schema install. Existing incompatible state is never converted."""
    if type(store) is not MutationStore:
        raise TypeError("native runtime requires the actual coordinator store")
    with store._transaction() as db:
        present = SQLiteSchemaObject.read(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE name=?",
                (NativeRuntimeSchemaMeta.declared_name,),
            )
        )
        if not present:
            schema = _schema()
            for statement in schema.values():
                db.execute(statement)
            NativeRuntimeSchemaMeta(1, 4, _digest(schema)).insert(db)
        assert_native_runtime_schema(db)
