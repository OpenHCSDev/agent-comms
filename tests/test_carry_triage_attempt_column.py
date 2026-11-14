"""A stored triage membership row without the attempt column carries as attempt 0."""
import sqlite3
from contextlib import closing
from pathlib import Path

from cutover.carry_triage_attempt_column import TriageAttemptColumn

from agent_comms.coordination.database import CoordinationStore
from agent_comms.execution.native_runtime_input import NativeRuntimeInput
from agent_comms.execution.triage_native_sources import TriageNativeSource
from agent_comms.nominal.field_codec import AssignmentId, InputId, ThreadLookup
from agent_comms.storage.locked_store import LiveFilesRequest

FIRST = InputId("a" * 32)
OWNER = ThreadLookup("c" * 32)


def old_store(root: Path) -> Path:
    root.mkdir(mode=0o700, parents=True)
    path = CoordinationStore.path_in(root)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(NativeRuntimeInput.schema_objects()[NativeRuntimeInput.declared_name])
        # The membership insert guard reads the claims it is attaching to.
        db.execute('CREATE TABLE "wake_claims" ("assignment_id" TEXT NOT NULL,'
                   ' "recipient_lookup" TEXT)')
        db.execute("INSERT INTO native_runtime_input (input_id, stage, owner_lookup,"
                   " owner_generation, owner_token_digest, expected_prompt_digest)"
                   " VALUES (?, 'triage', ?, 1, ?, ?)", (FIRST, OWNER, "0" * 64, "1" * 64))
        db.execute(f'CREATE TABLE "{TriageNativeSource.declared_name}" ('
                   ' "input_id" TEXT NOT NULL, "ordinal" INTEGER NOT NULL,'
                   ' "assignment_id" TEXT NOT NULL UNIQUE, PRIMARY KEY ("input_id","ordinal"))'
                   f' STRICT, WITHOUT ROWID')
        db.execute("INSERT INTO wake_claims VALUES ('claim-1', ?)", (OWNER,))
        db.execute(f'INSERT INTO "{TriageNativeSource.declared_name}" VALUES (?, 0, ?)',
                   (FIRST, "claim-1"))
    return path


def test_an_old_membership_row_carries_as_the_original_ask(tmp_path):
    root = tmp_path / "root"
    path = old_store(root)
    step = TriageAttemptColumn(LiveFilesRequest(root), tmp_path / "spill")

    assert step.carry(apply=False).detail == {"stores": 1}
    step.carry(apply=True)
    with closing(sqlite3.connect(path)) as db:
        assert TriageNativeSource.select(db) == [
            TriageNativeSource(FIRST, 0, AssignmentId("claim-1"), 0),
        ]
        assert "attempt" in TriageNativeSource.stored_columns(db)
    assert step.carry(apply=True).changed == 0
