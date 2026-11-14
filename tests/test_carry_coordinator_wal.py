"""A rollback-journal coordinator carries into WAL with its rows; a new one is born in WAL."""
import sqlite3
from contextlib import closing
from pathlib import Path

from cutover.carry_coordinator_wal import CoordinatorWal

from agent_comms.coordination.database import CoordinationStore
from agent_comms.storage.locked_store import LiveFilesRequest


def journal_mode(path: Path) -> str:
    with closing(sqlite3.connect(path)) as db:
        return db.execute("PRAGMA journal_mode").fetchone()[0]


def test_a_rollback_coordinator_carries_into_wal_keeping_its_rows(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    path = CoordinationStore.path_in(root)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE kept (value TEXT)")
        db.execute("INSERT INTO kept VALUES ('row')")
    assert journal_mode(path) == "delete"
    step = CoordinatorWal(LiveFilesRequest(root), tmp_path / "spill")

    assert step.carry(apply=False).detail == {"stores": 1}
    step.carry(apply=True)
    assert journal_mode(path) == "wal"
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT value FROM kept").fetchall() == [("row",)]
    assert step.carry(apply=True).changed == 0


def test_a_new_coordinator_is_published_in_wal(tmp_path):
    path = CoordinationStore.path_in(tmp_path)
    CoordinationStore(path).close()
    assert journal_mode(path) == "wal"
