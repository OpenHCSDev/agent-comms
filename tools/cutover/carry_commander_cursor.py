"""Add the rebuildable commander reader position without changing any order row.

Delete after installation has carried the existing buses.
"""
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.todos import CommanderCursor, TodoStore


class CommanderReaderPosition(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return tuple(path for path in SQLiteFile.files(TodoStore.path_in(root)) if path.exists())

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = TodoStore.path_in(root)
        if not path.exists():
            return Carried(0)
        cls.require_confined(root, path)
        with closing(CoordinationStore.read_snapshot(path)) as db:
            present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (CommanderCursor.declared_name,)).fetchone()
        if present:
            return Carried(0)
        if not apply:
            return Carried(1)
        with closing(CoordinationStore.writer(path, isolation_level=None)) as db:
            with SQLiteFile.write_transaction(db):
                CommanderCursor.create(db)
                TodoStore.require_schema(db)
        return Carried(1, rewritten=(str(path.relative_to(root)),))
