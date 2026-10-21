"""Install and verify the one current declaration-derived native schema."""

from __future__ import annotations

import sqlite3

from agent_comms.coordination_errors import PublicationActivationBlocked
from agent_comms.coordinator import Coordination

from .native_runtime_input import NativeRuntimeTable
from .typed_table import SQLiteForeignKeys


def assert_native_runtime_schema(db: sqlite3.Connection) -> None:
    if not NativeRuntimeTable.stored_schema(db):
        raise PublicationActivationBlocked("native runtime schema is not installed")
    if NativeRuntimeTable.schema_differs(db) or SQLiteForeignKeys.read(
        db.execute("PRAGMA foreign_keys")
    ) != [SQLiteForeignKeys(True)]:
        raise PublicationActivationBlocked("native runtime schema has drifted")


def install_native_runtime_schema(store: Coordination) -> None:
    """Explicit fresh schema install. Existing state must match the declared schema."""
    with store.session.writing() as db:
        if not NativeRuntimeTable.stored_schema(db):
            NativeRuntimeTable.create_schema(db)
        assert_native_runtime_schema(db)
