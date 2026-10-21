"""Install and verify the one current declaration-derived native schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from agent_comms.coordination_errors import PublicationActivationBlocked
from agent_comms.coordinator import Coordination

from .field_codec import MalformedRecord
from .native_runtime_input import NativeRuntimeSchemaMeta, NativeRuntimeTable
from .typed_table import SQLiteForeignKeys


def _digest(schema: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(schema, separators=(",", ":")).encode()).hexdigest()


def assert_native_runtime_schema(db: sqlite3.Connection) -> None:
    schema = NativeRuntimeTable.declared_schema()
    try:
        meta = NativeRuntimeSchemaMeta.one(db, singleton=1)
        drifted = NativeRuntimeTable.schema_differs(db)
    except (sqlite3.Error, MalformedRecord) as error:
        raise PublicationActivationBlocked("native runtime schema is not installed") from error
    if meta != NativeRuntimeSchemaMeta(singleton=1, ddl_digest=_digest(schema)):
        raise PublicationActivationBlocked("native runtime schema version differs")
    if drifted or SQLiteForeignKeys.read(
        db.execute("PRAGMA foreign_keys")
    ) != [SQLiteForeignKeys(True)]:
        raise PublicationActivationBlocked("native runtime schema has drifted")


def install_native_runtime_schema(store: Coordination) -> None:
    """Explicit fresh schema install. Existing state must match the declared schema."""
    with store.session.writing() as db:
        if not NativeRuntimeSchemaMeta.present(db):
            NativeRuntimeTable.create_schema(db)
            NativeRuntimeSchemaMeta(
                singleton=1, ddl_digest=_digest(NativeRuntimeTable.declared_schema())
            ).insert(db)
        assert_native_runtime_schema(db)
