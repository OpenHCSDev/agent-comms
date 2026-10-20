"""Install the Todo declaration's indexes without changing any saved row."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import carry

from agent_comms.todos import Todo


class TodoOpenIndexes(carry.TodoRows):

    @classmethod
    def carry_rows(cls, root: Path, db: sqlite3.Connection, *, apply: bool) -> carry.Carried:
        query = "SELECT name FROM sqlite_master WHERE type='index'"
        before = set(db.execute(query))
        Todo.create(db, if_not_exists=True)
        changed = len(set(db.execute(query)) - before)
        return cls.carried(root, changed, {"indexes": changed}, apply=apply)


if __name__ == "__main__":
    TodoOpenIndexes.main()
