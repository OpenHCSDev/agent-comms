"""Explicit installation may fill absent schemas, never repair an existing drift."""

import sqlite3

import pytest

from agent_comms.cohort_schema import CohortSchemaMeta
from agent_comms.comms import Comms
from agent_comms.coordination_errors import SchemaVersionError
from agent_comms.coordinator import Coordination
from agent_comms.threads import Thread


def test_drifted_private_owner_refused_and_readers_leave_missing_schema_alone(tmp_path):
    comms = Comms(tmp_path / "wire")
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.threads.register(Thread("owner", frozenset(), str(tmp_path)))
    comms.owners.pin_private_nk_launch(comms.root, root_id, tmp_path)
    database = comms.root / "coordination.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute(f'DROP TABLE "{CohortSchemaMeta.declared_name}"')
        before = db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    assert Comms(comms.root).views.full_history() == []
    with Coordination(str(database)):
        pass  # Opening a store is not explicit private-runtime installation.
    with pytest.raises(SchemaVersionError):
        comms.owners.start("owner")
    assert not comms.registry.require("owner").process_alive
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == before
