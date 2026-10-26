"""Explicit installation may fill absent schemas, never repair an existing drift."""



from agent_comms.repo import Checkout
import sqlite3

import pytest

from agent_comms.comms import wire
from agent_comms.maintenance_barrier import MaintenanceNotHeldError
from agent_comms.coordinator import Coordination
from bus_history import read_history
from agent_comms.threads import Thread


def test_drifted_private_owner_refused_and_readers_leave_missing_schema_alone(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.registry.prepare_native_root()
    comms.registry.declare(Thread('owner', frozenset(), Checkout(tmp_path)))
    database = comms.root / "coordination.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE claim_batch_members")
        before = db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    assert read_history(wire(comms.root)) == []
    with pytest.raises(MaintenanceNotHeldError):
        Coordination(str(database))
    with pytest.raises(MaintenanceNotHeldError):
        comms.owners.start("owner")
    assert not comms.registry.require("owner").process_alive
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == before
