"""A coordination database written before the connectivity facet was deleted loses it once."""

import sqlite3

from agent_comms.coordination_database import CoordinationStore
from conftest import maintenance_held
from cutover.carry_drop_connectivity import DropConnectivity


def test_carry_drops_the_table_and_its_triggers_once(tmp_path):
    path = tmp_path / CoordinationStore.filename
    with CoordinationStore(path):
        pass
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE connectivity (execution_id TEXT PRIMARY KEY, revision INTEGER)")
        db.execute(
            "CREATE TRIGGER connectivity_delete_before_abort BEFORE DELETE ON connectivity "
            "BEGIN SELECT RAISE(ABORT, 'no delete'); END"
        )
    step = DropConnectivity(tmp_path, tmp_path, tmp_path / "spill")

    with maintenance_held(tmp_path):
        assert step.carry(apply=False).changed == 1
        assert step.carry(apply=True).rewritten == (CoordinationStore.filename,)
        assert step.carry(apply=False).changed == 0
        with sqlite3.connect(path) as db:
            assert not db.execute(
                "SELECT name FROM sqlite_master WHERE tbl_name = 'connectivity'"
            ).fetchall()
        with CoordinationStore(path):
            pass
