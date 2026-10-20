"""Install the Todo declaration's indexes without changing any saved row."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from . import carry

from agent_comms.todos import Todo


class TodoOpenIndexes(carry.TodoRows):
    @classmethod
    def converted(cls, db: sqlite3.Connection) -> Mapping[str, int]:
        query = "SELECT name FROM sqlite_master WHERE type='index'"
        before = set(db.execute(query))
        Todo.create(db, if_not_exists=True)
        changed = len(set(db.execute(query)) - before)
        return {"indexes": changed}


if __name__ == "__main__":
    TodoOpenIndexes.main()
