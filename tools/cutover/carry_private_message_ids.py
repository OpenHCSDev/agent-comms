"""Rebuild the sealed private bus checkpoint with its declared message-ID indexes.

Run only after native owners have stopped. The existing checked prefix proves the
old bus before the current source rebuilds the index and publishes its new seal.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.private_bus_checkpoint import CheckedPrefix, CheckpointTable
from agent_comms.typed_table import SQLiteSchemaObject, TypedTable
from agent_comms.wire_log import WireLog

from .carry import Carried, Carry


class PrivateMessageIds(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        bus = WireLog(root / "bus.jsonl")
        return (root / "private_bus_checkpoint.sqlite3", bus.metadata_path)

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        checkpoint = root / "private_bus_checkpoint.sqlite3"
        if not checkpoint.exists():
            return Carried(0)
        with closing(CoordinationStore.read_snapshot(checkpoint)) as db:
            actual = SQLiteSchemaObject.read(
                db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")
            )
        expected = {
            name: sql
            for table in TypedTable.members_with(CheckpointTable)
            for name, sql in table.schema_objects().items()
        }
        differences = SQLiteSchemaObject.differences(actual, expected)
        if not differences:
            return Carried(0)
        if len(differences) != 2 or any(
            difference.stored
            or not difference.declared
            or not difference.declared.startswith("CREATE INDEX")
            for difference in differences
        ):
            raise ValueError("Private bus checkpoint differs beyond the two new message indexes")
        if apply:
            CheckedPrefix.reindex_messages(WireLog(root / "bus.jsonl"))
        return Carried(
            2,
            {"indexes": 2},
            tuple(str(path.relative_to(root)) for path in cls.backup_files(root)) if apply else (),
        )


if __name__ == "__main__":
    PrivateMessageIds.main()
