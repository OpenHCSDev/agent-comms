"""The trigger carry preserves application rows and refuses an unknown sidecar."""
import sqlite3
from contextlib import closing

import pytest

from cutover.carry_table_triggers import TableTriggers
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.coordination_tables.participants import Participants
from agent_comms.errors import RelationViolationError
from agent_comms.native_prompt_binding import PromptBinding
from agent_comms.private_sidecar import create_sidecar_file
from agent_comms.typed_table import SQLiteTableName, TableSchema


def saved_rows(db):
    return {table.name: tuple(db.execute(f'SELECT * FROM "{table.name}"'))
            for table in SQLiteTableName.stored(db)}


def test_trigger_rewrite_preserves_rows_and_is_idempotent(tmp_path):
    path = tmp_path / "coordination.sqlite3"
    with closing(CoordinationStore.writer(path)) as db:
        CoordinatorTable.create_schema(db)
        Participants(participant_lookup="owner", display_name="owner", committed=True).insert(db)
        (delete_guard,) = (guard for guard in Participants.triggers() if "_delete_" in guard)
        db.execute(f'DROP TRIGGER "{delete_guard}"')
        db.execute('CREATE TRIGGER previous_guard BEFORE DELETE ON participants '
                   "BEGIN SELECT RAISE(ABORT, 'frozen'); END")
        db.commit()
        before = saved_rows(db)
        schema = TableSchema.stored_schema(db)
    assert TableTriggers.rewrite(path, apply=False) == 1
    with closing(CoordinationStore.read_snapshot(path)) as db:
        assert TableSchema.stored_schema(db) == schema
    assert TableTriggers.rewrite(path, apply=True) == 1
    assert TableTriggers.rewrite(path, apply=True) == 0
    with closing(CoordinationStore.read_snapshot(path)) as db:
        assert not CoordinatorTable.schema_differs(db)
        assert saved_rows(db) == before
    with closing(CoordinationStore.writer(path)) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM participants")


def test_unknown_sidecar_is_not_blessed(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / PromptBinding.filename
    create_sidecar_file(path, PromptBinding)
    with closing(CoordinationStore.writer(path)) as db:
        db.execute('CREATE TRIGGER unknown_guard BEFORE INSERT ON prompt_binding BEGIN SELECT 1; END')
        db.commit()
        before = saved_rows(db)
        schema = TableSchema.stored_schema(db)
    with pytest.raises(RelationViolationError) as failure:
        TableTriggers.rewrite(path, apply=True)
    assert str(path) in str(failure.value)
    with closing(CoordinationStore.read_snapshot(path)) as db:
        assert saved_rows(db) == before
        assert TableSchema.stored_schema(db) == schema
