"""The real carry splits each triage input's JSON membership into one row per member."""

import sqlite3
from contextlib import closing
from pathlib import Path

from cutover.carry_triage_member_rows import TriageMemberRows

from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import AssignmentId, InputId
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.triage_native_sources import TriageNativeSource

FIRST, SECOND = InputId("a" * 32), InputId("b" * 32)


def old_triage_store(root: Path) -> Path:
    root.mkdir(mode=0o700, parents=True)
    path = CoordinationStore.path_in(root)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(NativeRuntimeInput.schema_objects()[NativeRuntimeInput.declared_name])
        db.executemany(
            "INSERT INTO native_runtime_input (input_id, stage, owner_lookup, owner_generation,"
            " owner_token_digest) VALUES (?, 'triage', 'owner', 1, ?)",
            [(FIRST, "0" * 64), (SECOND, "0" * 64)])
        db.execute('CREATE TABLE "native_runtime_triage_sources" ("input_id" TEXT NOT NULL,'
                   ' "assignment_ids" TEXT NOT NULL, PRIMARY KEY ("input_id")) WITHOUT ROWID')
        db.executemany("INSERT INTO native_runtime_triage_sources VALUES (?, ?)",
                       [(FIRST, '["claim-1","claim-2"]'), (SECOND, '["claim-3"]')])
    return path


def test_carry_splits_membership_into_rows_once(tmp_path):
    root = tmp_path / "root"
    path = old_triage_store(root)
    step = TriageMemberRows(root, root, tmp_path / "spill")

    assert step.carry(apply=False).detail == {"members": 3}
    assert step.carry(apply=True).rewritten == (path.relative_to(root),)
    with closing(sqlite3.connect(path)) as db:
        assert sorted(TriageNativeSource.select(db), key=lambda row: row.assignment_id) == [
            TriageNativeSource(FIRST, 0, AssignmentId("claim-1")),
            TriageNativeSource(FIRST, 1, AssignmentId("claim-2")),
            TriageNativeSource(SECOND, 0, AssignmentId("claim-3")),
        ]
        stored = {name for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL",
            (TriageNativeSource.declared_name,))}
        assert stored == set(TriageNativeSource.schema_objects())
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='native_runtime_triage_sources'").fetchone()
    assert step.carry(apply=True).changed == 0
