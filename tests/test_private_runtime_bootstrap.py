"""Explicit installation may fill absent schemas, never repair an existing drift."""

from agent_comms.bus import Bus

import sqlite3

import pytest

from agent_comms.comms import Comms
from agent_comms.coordination_errors import SchemaVersionError
from agent_comms.coordinator import Coordination
from bus_history import read_history


def test_drifted_private_owner_refused_and_readers_leave_missing_schema_alone(tmp_path):
    comms = Comms(tmp_path / "wire")
    Bus.install(Bus.path_in(comms.root))
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(tmp_path)))
    database = comms.root / "coordination.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE claim_batch_members")
        before = db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    assert read_history(Comms(comms.root)) == []
    with Coordination(str(database)):
        pass  # Opening a store is not explicit private-runtime installation.
    with pytest.raises(SchemaVersionError):
        comms.owners.start("owner")
    assert not comms.registry.require("owner").process_alive
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == before
