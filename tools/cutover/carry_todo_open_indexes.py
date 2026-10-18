"""Install the Todo declaration's indexes without changing any saved row."""

from __future__ import annotations

from pathlib import Path

from cutover import carry

from agent_comms.todos import Todo


class TodoOpenIndexes(carry.TodoRows):
    after = "carry_todo_corrections"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> carry.Carried:
        with cls.todo_rows(root, apply=apply) as db:
            if db is None:
                return carry.Carried(0)
            query = "SELECT name FROM sqlite_master WHERE type='index'"
            before = set(db.execute(query))
            Todo.create(db, if_not_exists=True)
            changed = len(set(db.execute(query)) - before)
        return cls.carried(root, changed, {"indexes": changed}, apply=apply)


if __name__ == "__main__":
    TodoOpenIndexes.main()
