"""The real carry renames the stored input_id column to slot_id and keeps every row."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from cutover.carry_selected_slot_column import SelectedSlotColumn

from agent_comms.coordination_database import CoordinationStore
from agent_comms.locked_store import LiveFilesRequest
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import SlotId
from agent_comms.native_runtime_input import SelectedToolSlot
from agent_comms.native_tool_call import ToolCallId


def old_slot_store(root: Path) -> Path:
    root.mkdir(mode=0o700, parents=True)
    path = CoordinationStore.path_in(root)
    with closing(sqlite3.connect(path)) as db, db:
        SelectedToolSlot.create(db)
        SelectedToolSlot(SlotId("a" * 32), ToolCallId("call-1")).insert(db)
        db.execute(f'ALTER TABLE "{SelectedToolSlot.declared_name}" '
                   'RENAME COLUMN slot_id TO input_id')
    return path


def test_carry_renames_the_stored_column_once(tmp_path):
    root = tmp_path / "root"
    path = old_slot_store(root)
    step = SelectedSlotColumn(LiveFilesRequest(root), tmp_path / "spill")

    assert step.carry(apply=False).changed == 1
    with closing(sqlite3.connect(path)) as db:
        assert "input_id" in SelectedToolSlot.stored_columns(db)

    assert step.carry(apply=True).rewritten == (path.relative_to(root),)
    with closing(sqlite3.connect(path)) as db:
        slot = SelectedToolSlot(SlotId("a" * 32), ToolCallId("call-1"))
        assert SelectedToolSlot.select(db) == [slot]
    assert step.carry(apply=True).changed == 0


def test_carry_refuses_a_table_holding_both_columns(tmp_path):
    root = tmp_path / "root"
    path = old_slot_store(root)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f'ALTER TABLE "{SelectedToolSlot.declared_name}" ADD COLUMN slot_id TEXT')

    with pytest.raises(RelationViolationError):
        SelectedSlotColumn(LiveFilesRequest(root), tmp_path / "spill").carry(apply=False)
