"""Install and verify the one current declaration-derived native schema."""

from __future__ import annotations

import sqlite3

from agent_comms.coordination_errors import PublicationActivationBlocked
from agent_comms.coordinator import Coordination

from .native_runtime_input import NativeRuntimeTable
from .typed_table import SQLiteForeignKeys


def install_native_runtime_schema(store: Coordination) -> None:
    """Explicit fresh schema install. Existing state must match the declared schema."""
    with store.session.writing() as db:
        if not NativeRuntimeTable.stored_schema(db):
            NativeRuntimeTable.create_schema(db)
        NativeRuntimeTable.require_installed(db)
