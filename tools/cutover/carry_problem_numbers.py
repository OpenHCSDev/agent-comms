"""Index original Todo identities and install allocation for unsent numbers.

Delete this step once every retained bus has been carried.
"""

from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.problem_number import ProblemNumber
from agent_comms.todos import ProblemAllocation, Todo, TodoStore


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
            numbers_missing = ProblemAllocation.declared_name not in stored
            migrate_numbers = numbers_missing and "problem_number" in stored
            missing_indexes = tuple(
                index
                for index in (Todo.unit_index, Todo.problem_index)
                if index.name(Todo) not in stored
            )
            changed = int(numbers_missing) + len(missing_indexes)
            if not changed:
                return Carried(0)
            created = tuple(ProblemNumber.parse(todo.id) for todo in Todo.stream(db))
        if not self.apply:
            return Carried(changed)
        with closing(CoordinationStore.writer(path)) as db, SQLiteFile.write_transaction(db):
            if migrate_numbers:
                # SQLite renames its sequence too; even unsent issued IDs stay reserved.
                db.execute(
                    f'ALTER TABLE problem_number RENAME TO "{ProblemAllocation.declared_name}"'
                )
                for number in created:
                    ProblemAllocation.delete(db, number=number.number)
            elif numbers_missing:
                ProblemAllocation.create(db)
            for index in missing_indexes:
                db.execute(index.sql(Todo))
            TodoStore.require_schema(db)
        return Carried(
            changed, rewritten=tuple(str(file.relative_to(root)) for file in SQLiteFile.files(path))
        )
