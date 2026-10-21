"""Seed global problem allocation and index the original Todo assignment.

Delete this step once every retained bus has been carried.
"""

from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.todos import ProblemNumber, Todo, TodoStore


class ProblemNumbers(Carry):
    def backup_files(self) -> tuple[Path, ...]:
        paths = tuple(
            path for path in SQLiteFile.files(TodoStore.path_in(self.root)) if path.exists()
        )
        for path in paths:
            self.require_confined(self.root, path)
        return paths

    def carry(self) -> Carried:
        root = self.root
        path = TodoStore.path_in(root)
        self.require_confined(root, path)
        if not path.exists():
            return Carried(0)
        with closing(CoordinationStore.read_snapshot(path)) as db:
            stored = {row.name for row in TodoStore.stored_schema(db)}
            numbers_missing = ProblemNumber.declared_name not in stored
            index_missing = Todo.unit_index.name(Todo) not in stored
        changed = int(numbers_missing) + int(index_missing)
        if not changed or not self.apply:
            return Carried(changed)
        with closing(CoordinationStore.writer(path)) as db, SQLiteFile.write_transaction(db):
            if numbers_missing:
                ProblemNumber.create(db)
                for todo in Todo.stream(db):
                    ProblemNumber.reserve(db, todo.id)
            if index_missing:
                db.execute(Todo.unit_index.sql(Todo))
            TodoStore.require_schema(db)
        return Carried(
            changed, rewritten=tuple(str(file.relative_to(root)) for file in SQLiteFile.files(path))
        )
