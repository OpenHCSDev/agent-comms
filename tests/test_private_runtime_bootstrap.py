"""Explicit installation may fill absent schemas, never repair an existing drift."""



import sqlite3

import pytest

from agent_comms.comms import wire
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import SchemaVersionError
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from bus_history import read_history


def test_drifted_private_owner_refused_and_readers_leave_missing_schema_alone(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.registry.store.prepare_native_root()
    comms.registry.declare(NativeThread('owner', frozenset(), Checkout(tmp_path)))
    database = comms.root / "coordination.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE claim_batch_members")
        before = db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    assert read_history(wire(comms.root)) == []
    with pytest.raises(SchemaVersionError):
        Coordination(str(database))
    with pytest.raises(SchemaVersionError):
        comms.owners.start_many(("owner",))
    assert not comms.registry.require("owner").process_alive
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == before
