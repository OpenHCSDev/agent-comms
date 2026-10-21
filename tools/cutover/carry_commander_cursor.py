"""Add the rebuildable commander reader and rename retained accepted head verdicts.

Delete after installation has carried the existing buses.
"""
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.todos import AcceptedHeadVerdict, CommanderCursor, Todo, TodoStore


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
        old_kind = "reviewed_head_verdict"
        with closing(CoordinationStore.read_snapshot(path)) as db:
            present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (CommanderCursor.declared_name,)).fetchone()
            verdicts = db.execute(
                f'SELECT count(*) FROM "{Todo.declared_name}", json_each(verdicts) '
                "WHERE json_extract(value, '$.kind')=?", (old_kind,)).fetchone()[0]
        changed = int(not present) + verdicts
        if not changed or not apply:
            return Carried(changed)
        with closing(CoordinationStore.writer(path, isolation_level=None)) as db:
            with SQLiteFile.write_transaction(db):
                if not present:
                    CommanderCursor.create(db)
                db.execute(
                    f'UPDATE "{Todo.declared_name}" SET verdicts=('
                    "SELECT json_group_array(json(CASE WHEN json_extract(value, '$.kind')=? "
                    "THEN json_set(value, '$.kind', ?) ELSE value END)) FROM json_each(verdicts)) "
                    "WHERE EXISTS (SELECT 1 FROM json_each(verdicts) "
                    "WHERE json_extract(value, '$.kind')=?)",
                    (old_kind, AcceptedHeadVerdict.declared_name, old_kind))
                TodoStore.require_schema(db)
        return Carried(changed, rewritten=(str(path.relative_to(root)),))

    @classmethod
    def before_stop(cls, root: Path, *, apply: bool) -> Carried:
        return cls.carry(root, apply=apply)
