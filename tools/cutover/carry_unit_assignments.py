"""Install the assignment row declaration without changing retained orders or inputs.

Delete this step once every retained bus has been carried.
"""
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.todos import ProblemNumber, Todo, TodoStore, UnitAssignment


class UnitAssignments(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        paths = tuple(path for path in SQLiteFile.files(TodoStore.path_in(root)) if path.exists())
        for path in paths:
            cls.require_confined(root, path)
        return paths

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = TodoStore.path_in(root)
        cls.require_confined(root, path)
        if not path.exists():
            return Carried(0)
        with closing(CoordinationStore.read_snapshot(path)) as db:
            stored = {row.name for row in TodoStore.stored_schema(db)}
            missing = tuple(row for row in (UnitAssignment, ProblemNumber)
                            if row.declared_name not in stored)
        if not missing or not apply:
            return Carried(len(missing))
        with closing(CoordinationStore.writer(path)) as db, SQLiteFile.write_transaction(db):
            for row in missing:
                row.create(db)
            for todo in Todo.stream(db):
                ProblemNumber.reserve(db, todo.id)
            TodoStore.require_schema(db)
        return Carried(len(missing), rewritten=tuple(
            str(file.relative_to(root)) for file in SQLiteFile.files(path)))
