"""The real carry gives saved hunts recorded before HuntId an id HuntId accepts."""

import sqlite3
from contextlib import closing

from cutover.carry_hunt_record_ids import HuntRecordIds

from agent_comms.change_report import FileLine
from agent_comms.locked_store import LiveFilesRequest
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.todos import CleanHunt, HuntRecord, TodoStore
from agent_comms.typed_table import SQLiteCatalogObject
from conftest import maintenance_held

COMMANDER = RecordedThreadIncarnation("claude-code", 1.0)


def test_carry_reissues_hunt_ids_recorded_before_hunt_id(tmp_path):
    store = TodoStore(tmp_path)
    hunts = tuple(
        store.record_hunt(COMMANDER, CleanHunt(
            FileLine("src/agent_comms/todos.py", line),
            "read the hunt table and found nothing worth ordering this time",
            RecordedEventTime(1.0)))
        for line in (1, 2))
    path = TodoStore.path_in(tmp_path)
    with closing(sqlite3.connect(path)) as db, db:
        SQLiteCatalogObject.release_guards(db, HuntRecord)
        db.execute(f'UPDATE "{HuntRecord.declared_name}" SET id=? WHERE id=?', ("P5:0", hunts[0].id))
    step = HuntRecordIds(LiveFilesRequest(tmp_path), tmp_path / "spill")

    assert step.carry(apply=False).changed == 1
    with maintenance_held(tmp_path):
        assert step.carry(apply=True).rewritten == (path.relative_to(tmp_path),)
    assert sorted(hunt.where.line for hunt in store.hunts_for_commander(COMMANDER)) == [1, 2]
    assert step.carry(apply=False).changed == 0
